"""Stage handlers, registered on the JobQueue by ``register_all``: `probe`,
`transcribe`, `detect`, `proxy` and `makeclip` (`download` is registered in server/app.py).

A handler gets a JobContext, reports progress, writes its results to the db and returns a
one-line summary for the job row. Raising marks the job failed (and never kills the lane).
"""

import json
import logging
import os
import time
from pathlib import Path

from core.library import rec_to_dict
from core.messages import msg
from media import ffmpeg

logger = logging.getLogger("wclips.stages")


def _set_stage(ctx, name: str, state: str, **extra) -> dict:
    """Read-modify-write of recordings.stages (kept for callers outside the stages; racy when two
    lanes finish on one recording at once -- the stages below use STAGE_SQL instead)."""
    rec = ctx.db.one("SELECT stages FROM recordings WHERE id=?", (ctx.rec["id"],))
    try:
        stages = json.loads((rec or {}).get("stages") or "{}")
    except json.JSONDecodeError:
        stages = {}
    stages[name] = {"state": state, "utc": time.time(), **extra}
    return stages


# one stage's entry written atomically inside the UPDATE, so detect (video lane) and transcribe
# (stt lane) finishing on the same recording at the same moment never drop each other's entry
STAGE_SQL = "stages=json_set(CASE WHEN json_valid(stages) THEN stages ELSE '{}' END, ?, json(?))"


def stage_params(name: str, state: str, **extra) -> tuple[str, str]:
    return f"$.{name}", json.dumps({"state": state, "utc": time.time(), **extra})


def probe(ctx) -> str:
    rec = ctx.rec
    path = rec["path"]
    if not os.path.exists(path):
        ctx.db.execute("UPDATE recordings SET missing=1, updated_utc=? WHERE id=?", (time.time(), rec["id"]))
        raise RuntimeError(msg("server.job.file_gone", "The file is not in its folder anymore.")["message"])
    ctx.progress(0.1, msg("server.job.probe_info", "Reading the file info")["message"])
    try:
        info = ffmpeg.probe(path)
        if not info.get("duration_s"):
            raise ffmpeg.ProbeError(msg("server.job.no_duration", "The recording has no length yet. OBS may still be "
                                         "writing it.")["message"])
        ctx.progress(0.6, msg("server.job.probe_keyint", "Finding the keyframe interval")["message"])
        ki = ffmpeg.keyint(path)
    except ffmpeg.ProbeError as e:
        ctx.db.execute(f"UPDATE recordings SET status='error', error=?, {STAGE_SQL}, updated_utc=? WHERE id=?",
                       (str(e)[:300], *stage_params("probe", "failed", error=str(e)[:200]), time.time(), rec["id"]))
        raise
    now = time.time()
    hud = f"{info['width']}x{info['height']}" if info.get("width") else ""
    ctx.db.execute(
        "UPDATE recordings SET duration_s=?, fps=?, width=?, height=?, vcodec=?, pix_fmt=?, bitrate=?, keyint_s=?,"
        f" audio_tracks=?, hud_profile=?, status='probed', error=NULL, {STAGE_SQL}, updated_utc=? WHERE id=?",
        (info["duration_s"], info["fps"], info["width"], info["height"], info["vcodec"], info["pix_fmt"],
         info["bitrate"], ki, json.dumps(info["audio_tracks"]), hud,
         *stage_params("probe", "done"), now, rec["id"]))
    ctx.db.execute("DELETE FROM tracks WHERE rec_id=?", (rec["id"],))
    ctx.db.executemany(
        "INSERT INTO tracks (rec_id, idx, role, codec, sample_rate, channels) VALUES (?,?,?,?,?,?)",
        [(rec["id"], t["idx"], "unknown", t["codec"], t["sample_rate"], t["channels"]) for t in info["audio_tracks"]])
    fresh = ctx.db.one("SELECT * FROM recordings WHERE id=?", (rec["id"],))
    ctx.bus.publish("recording", action="updated", rec=rec_to_dict(fresh))
    # follow-ups only on a recording's FIRST successful probe: a re-probe (a full rescan, Re-probe,
    # Probe all after an error) refreshes metadata and must not queue the whole library's backlog
    try:
        was = json.loads(rec.get("stages") or "{}").get("probe") or {}
    except (json.JSONDecodeError, AttributeError):
        was = {}
    follow = [] if was.get("state") == "done" else _queue_followups(ctx, fresh)
    ctx.progress(1.0, msg("server.job.done", "Done")["message"])
    facts = {"duration": f"{info['duration_s']:.0f}", "width": info["width"], "height": info["height"],
             "codec": info["vcodec"], "fps": f"{(info['fps'] or 0):.0f}", "tracks": len(info["audio_tracks"]),
             "keyint": ki}
    if follow:
        return msg("server.job.probe_done_queued", "{duration} s, {width}x{height} {codec}, {fps} fps, {tracks} audio "
                   "tracks, keyframe every {keyint} s. Queued next: {stages}.", stages=", ".join(follow),
                   **facts)["message"]
    return msg("server.job.probe_done", "{duration} s, {width}x{height} {codec}, {fps} fps, {tracks} audio tracks, "
               "keyframe every {keyint} s.", **facts)["message"]


def _mmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _queue_followups(ctx, rec: dict) -> list[str]:
    """After a probe: queue transcribe / detect (Settings > Jobs auto_transcribe / auto_detect) so
    new footage is ready without a click. A re-probe of a recording that already has them does not
    redo them; enqueue dedupes against queued / running jobs."""
    from core.library import _priority, parse_name
    try:
        stages = json.loads(rec.get("stages") or "{}")
    except json.JSONDecodeError:
        stages = {}
    pri = _priority(parse_name(rec["name"])[1], rec.get("mtime") or 0)
    out = []
    want = [("transcribe", ctx.cfg.jobs.auto_transcribe and ctx.cfg.stt.enabled),
            ("detect", ctx.cfg.jobs.auto_detect)]
    for stage, on in want:
        if not on or (stages.get(stage) or {}).get("state") == "done":
            continue
        if ctx.queue.enqueue(rec["id"], stage, priority=pri) is not None:
            out.append(stage)
    if out:
        logger.info("[Stages] probe done, queued %s rec=%s", "+".join(out), rec["id"])
    return out


def transcribe(ctx) -> str:
    """Measure the audio tracks (roles), extract the voice track, run faster-whisper, store
    the segments. Re-running replaces the previous transcript."""
    from core import transcripts
    from media import audio
    from stt import whisper as stt
    from utils import paths

    cfg, rec = ctx.cfg, ctx.rec
    t_job = time.monotonic()                         # whole stage incl. extraction: the "about N h" estimate
    if not cfg.stt.enabled:
        raise RuntimeError(msg("server.job.stt_disabled", "Speech-to-text is disabled (Settings > Speech-to-text).")["message"])
    if rec["status"] != "probed" or not rec.get("duration_s"):
        raise RuntimeError(msg("server.probe_first", "This recording has not been read yet. Wait until its probe job is done.")["message"])
    path, duration = rec["path"], float(rec["duration_s"])
    if not os.path.exists(path):
        ctx.db.execute("UPDATE recordings SET missing=1, updated_utc=? WHERE id=?", (time.time(), rec["id"]))
        raise RuntimeError(msg("server.job.file_gone", "The file is not in its folder anymore.")["message"])
    if not stt.available():
        raise RuntimeError(msg("server.job.no_whisper", "The speech-to-text engine is missing. Reinstall WardogClips.")["message"])
    try:
        tracks = json.loads(rec.get("audio_tracks") or "[]")
    except json.JSONDecodeError:
        tracks = []
    if not tracks:
        note = msg("server.job.no_audio", "This recording has no audio tracks.")["message"]
        _finish_transcribe(ctx, [], None, note)
        return note

    # resume point (core/checkpoints.py): a transcribe cut short (app closed, stopped, crashed) keeps its wavs and the
    # lines so far, and the next run goes on from the end of the last segment whisper finished
    from core import checkpoints
    tag = f"rec{rec['id']}"
    lay = audio.obs_layout(cfg, rec)              # a stream VOD: one mixed track, no OBS names / overrides
    hot = ", ".join(x for x in (cfg.player.aliases + cfg.player.squad +
                                [h.strip() for h in cfg.stt.hotwords.split(";")]) if x)
    mic_override = cfg.audio.mic_track if rec.get("kind") != "vod" else -1
    # speech language (core/speechlang.py): the recording's own override, else Settings; any non-English language
    # swaps an English-only model for its multilingual twin
    from core import speechlang
    lang_setting = speechlang.for_recording(cfg, rec)
    lp = speechlang.parse(lang_setting)
    model = speechlang.effective_model(cfg.stt.model, lang_setting)
    redetect = lp["mode"] == "auto" and bool(cfg.stt.redetect_windows)
    translate = bool(cfg.stt.translate) and speechlang.needs_multilingual(lang_setting)
    fp = {"file": checkpoints.file_stamp(path), "duration": duration, "tracks": len(tracks), "model": model,
          "compute": cfg.stt.compute, "hot": hot, "words": bool(cfg.stt.word_timestamps), "vad": bool(cfg.stt.vad),
          "lay": {k: lay[k] for k in ("voice", "game", "voip", "normalize_mic", "names") if k in lay},
          "mic": mic_override, "v": transcripts.TRANSCRIBE_VERSION, "lang": lang_setting,
          **({"redetect": 1} if redetect else {}), **({"translate": 1} if translate else {}),
          **({"batched": int(cfg.stt.batch_size)} if cfg.stt.batched else {}),
          "code": checkpoints.code_stamp("core/stages.py", "stt/whisper.py", "media/audio.py", "core/speechlang.py")}
    saved = checkpoints.load("transcribe", rec["id"], fp)
    if saved and not all(os.path.exists(f) for f in saved["files"]):
        logger.info("[STT] rec=%s checkpoint wavs are gone -- starting over", rec["id"])
        checkpoints.clear("transcribe", rec["id"], state=saved)
        saved = None
    rows, n_words, dropped = [], 0, 0
    whisper_s = job_s = 0.0                       # whisper / whole-stage wall time of earlier runs
    done_k, cut = 0, 0.0                          # tracks finished, and how far into track done_k whisper got
    keep_wavs = False
    track_langs: dict[int, list] = {}             # track -> [language, detection probability (None = fixed)]
    if saved:
        track_langs = {int(k): v for k, v in (saved.get("track_langs") or {}).items()}
        speech = [tuple(x) for x in saved["speech"]]
        specs = [(i, Path(w), n) for i, w, n in saved["specs"]]
        raw_shout = Path(saved["raw_shout"]) if saved["raw_shout"] else None
        rows, n_words, dropped = saved["rows"], saved["n_words"], saved["dropped"]
        whisper_s, job_s, done_k, cut = saved["whisper_s"], saved["job_s"], saved["done_k"], saved["cut"]
        track_rows = {int(k): v for k, v in saved["track_rows"].items()}
        logger.info("[STT] rec=%s resuming at track %d %.1fs (%d lines kept)", rec["id"],
                    speech[min(done_k, len(speech) - 1)][0] + 1, cut, len(rows))
        ctx.progress(0.30 + 0.69 * (done_k + min(1.0, cut / duration)) / len(speech),
                     msg("server.job.resuming", "Going on from {pos}", pos=_mmss(cut))["message"])
    else:
        # 1. which tracks carry speech: your mic, the squad (Discord), or an unnamed voice / mix track
        ctx.progress(0.02, msg("server.job.measuring", "Measuring the audio tracks")["message"])
        measures = audio.measure_tracks(path, duration, len(tracks), paths.CACHE_DIR, tag)
        roles = audio.decide_roles(measures, lay["voice"], lay["game"], names=lay["names"],
                                   mic_override=mic_override, voip_override=lay["voip"])
        track_rows = {i: [role, measures[i]["speech_share"], measures[i]["rms_db"]] for i, role in roles.items()}
        speech = audio.speech_tracks(roles)
        logger.info("[STT] roles rec=%s %s", rec["id"], " ".join(f"t{i}={r}" for i, r in sorted(roles.items())))
        if not speech:
            note = msg("server.job.no_speech", "no speech found on any track")["message"]
            _finish_transcribe(ctx, [], None, note, secs=time.monotonic() - t_job)
            return note
        # 2. every speech track as a wav, in one read of the file; the quiet mic normalized for whisper,
        #    and also raw for the excitement envelope (the normalizer would flatten the shouting)
        norm = bool(lay["normalize_mic"])            # never for a VOD's mixed track (and no raw second copy)
        specs = []
        for i, role in speech:
            specs.append((i, paths.CACHE_DIR / f"{tag}_t{i}.wav", norm and role in ("mic", "voice")))
        raw_shout = None
        if norm and speech[0][1] in ("mic", "voice"):
            raw_shout = paths.CACHE_DIR / f"{tag}_t{speech[0][0]}_raw.wav"
            specs.append((speech[0][0], raw_shout, False))
    # the roles (again on a resume: a re-probe in between rewrites the tracks rows as 'unknown')
    for i, (role, share, rms) in track_rows.items():
        ctx.db.execute("UPDATE tracks SET role=?, speech_share=?, rms_mean_db=? WHERE rec_id=? AND idx=?",
                       (role, share, rms, rec["id"], i))
    wav_for = {i: w for i, w, _n in specs if w != raw_shout}
    shout_track = speech[0][0]                       # mic first (you shouting), else the squad / mix
    label = ", ".join(f"{i + 1} ({audio.SPEAKER.get(r) or r})" for i, r in speech)
    in_seg, whisper_t0 = False, None

    def _checkpoint():
        if in_seg or whisper_t0 is None:
            return False                             # half-way through a segment, or the wavs are not there yet
        return checkpoints.save("transcribe", rec["id"], fp, {
            "speech": [list(x) for x in speech], "specs": [[i, str(w), n] for i, w, n in specs],
            "raw_shout": str(raw_shout) if raw_shout else None, "files": [str(w) for _i, w, _n in specs],
            "track_rows": {str(i): v for i, v in track_rows.items()},
            "track_langs": {str(i): v for i, v in track_langs.items()},
            "rows": rows, "n_words": n_words, "dropped": dropped,
            "whisper_s": whisper_s + time.monotonic() - whisper_t0, "job_s": job_s + time.monotonic() - t_job,
            "done_k": done_k, "cut": cut})

    try:
        if not saved:
            audio.extract_tracks(path, specs, duration,
                                 on_progress=lambda f: ctx.progress(0.05 + 0.25 * f, msg(
                                     "server.job.extracting", "Copying out audio tracks {tracks}",
                                     tracks=label)["message"]))
        # the footage has been read: free the drive slot so a detect on the same (HDD) drive can start
        # while whisper runs (minutes of GPU compute that never touch the footage)
        release = getattr(ctx, "release_drive", None)
        if release:
            release()
        # 3. whisper, one track at a time, every line tagged with its track and speaker
        whisper_t0 = time.monotonic()
        next_save = whisper_t0 + checkpoints.CHECKPOINT_EVERY_S
        try:
            for k, (i, role) in enumerate(speech):
                if k < done_k:
                    continue
                speaker = audio.SPEAKER.get(role, "")
                offset = cut if k == done_k else 0.0
                batch_kw = {"batched": True, "batch_size": int(cfg.stt.batch_size)} if cfg.stt.batched else {}
                # the track's language: fixed by the setting, else detected once on its speech (kept on a resume)
                if i not in track_langs:
                    ctx.progress(0.30 + 0.69 * k / len(speech), msg("server.job.detecting_language",
                                                                    "Listening for the language of track {n}",
                                                                    n=i + 1)["message"])
                    track_langs[i] = _track_language(stt, wav_for[i], model, cfg, lp, rec["id"], i)
                lang = track_langs[i][0]
                for seg in stt.engine.transcribe(wav_for[i], model=model, compute=cfg.stt.compute, hotwords=hot,
                                                 word_timestamps=cfg.stt.word_timestamps, vad=cfg.stt.vad,
                                                 language=lang, **({"multilingual": True} if redetect else {}),
                                                 **({"offset_s": offset} if offset else {}), **batch_kw):
                    in_seg = True
                    if lang is None and not redetect and seg.get("lang"):
                        # nothing detected up front: the language whisper settled on is the track's
                        lang = seg["lang"]
                        track_langs[i] = [lang, None]
                    seg = {**seg, "lang": seg.get("lang") or lang}
                    if stt.is_junk(seg, hot):
                        dropped += 1
                    else:
                        for piece in stt.split_at_gaps(seg):
                            piece["track"], piece["speaker"] = i, speaker
                            rows.append(piece)
                            n_words += len(piece.get("words") or []) or len(piece["text"].split())
                    cut, in_seg = max(cut, float(seg["t1"])), False
                    frac = (k + min(1.0, seg["t1"] / duration)) / len(speech)
                    if speaker:
                        line = msg("server.job.transcribing_speaker", "Transcribing track {n} ({speaker}) with {model}: "
                                   "{pos} of {total}", n=i + 1, speaker=speaker, model=model, pos=_mmss(seg["t1"]),
                                   total=_mmss(duration))
                    else:
                        line = msg("server.job.transcribing", "Transcribing track {n} with {model}: {pos} of {total}",
                                   n=i + 1, model=model, pos=_mmss(seg["t1"]), total=_mmss(duration))
                    ctx.progress(0.30 + 0.69 * frac, line["message"])
                    if ctx.stopping():
                        raise RuntimeError(msg("server.job.stopped_transcribing", "Stopped while transcribing.")["message"])
                    if time.monotonic() >= next_save:
                        _checkpoint()
                        next_save = time.monotonic() + checkpoints.CHECKPOINT_EVERY_S
                # optional English translation of the track's non-English lines, stored beside them
                todo = [r for r in rows if r.get("track") == i and r.get("lang") not in (None, "", "en")
                        and not r.get("text_en")] if translate else []
                if todo:
                    ctx.progress(0.30 + 0.69 * (k + 0.99) / len(speech), msg(
                        "server.job.translating", "Translating track {n} to English", n=i + 1)["message"])
                    tsegs = []
                    for ts in stt.engine.transcribe(wav_for[i], model=model, compute=cfg.stt.compute, hotwords=hot,
                                                    word_timestamps=False, vad=cfg.stt.vad,
                                                    language=lang if lang and lang != "en" else None,
                                                    task="translate", **({"multilingual": True} if redetect else {}),
                                                    **batch_kw):
                        if not stt.is_junk(dict(ts, lang="en"), hot):
                            tsegs.append(ts)
                        if ctx.stopping():
                            raise RuntimeError(msg("server.job.stopped_translating", "Stopped while translating.")["message"])
                    n_tr = transcripts.attach_translations(todo, tsegs)
                    logger.info("[STT] translated rec=%s track=%d lines=%d of %d", rec["id"], i, n_tr, len(todo))
                done_k, cut = k + 1, 0.0
        except BaseException:
            # the next run goes on from here with these wavs -- kept only while a checkpoint names them (else
            # nothing would ever find and remove them)
            keep_wavs = _checkpoint() or checkpoints.path_for("transcribe", rec["id"]).exists()
            raise
        elapsed = whisper_s + time.monotonic() - whisper_t0
        per_track = {}
        for r in rows:
            per_track[r["track"]] = per_track.get(r["track"], 0) + 1
        # 4. excitement spikes from the voice level -- now, while the wavs still exist
        ctx.progress(0.99, msg("server.job.voice_highlights", "Finding voice highlights")["message"])
        excitement = []
        try:
            from core import highlights
            excitement = highlights.excitement_events(audio.read_wav(raw_shout or wav_for[shout_track]),
                                                      audio.SAMPLE_RATE, cfg.highlights.rms_spike_db)
        except Exception as e:  # noqa: BLE001 - the highlight pass must never cost the transcript
            logger.warning("[Voice] excitement pass failed rec=%s err=%s", rec["id"], e)
    finally:
        if not keep_wavs:
            for _i, w, _n in specs:
                try:
                    os.remove(w)
                except OSError:
                    pass
    rows.sort(key=lambda r: r["t0"])
    for i, (lng, prob) in track_langs.items():
        ctx.db.execute("UPDATE tracks SET lang=?, lang_prob=? WHERE rec_id=? AND idx=?", (lng, prob, rec["id"], i))
    transcripts.replace_segments(ctx.db, rec["id"], rows, model)
    speech_s = sum(r["t1"] - r["t0"] for r in rows)

    def _tl(i):
        lng, prob = (track_langs.get(i) or [None, None])[:2]
        return {"lang": lng, **({"lang_prob": prob} if prob is not None else {})}
    vres = _finish_transcribe(ctx, rows, speech[0][0], None, model=model, excitement=excitement,
                              tracks={str(i): {"role": r, "lines": per_track.get(i, 0), **_tl(i)} for i, r in speech},
                              secs=job_s + time.monotonic() - t_job, resumed=bool(saved), language=lang_setting,
                              translated=sum(1 for r in rows if r.get("text_en")) if translate else None)
    checkpoints.clear("transcribe", rec["id"], keep_files=True)
    ctx.progress(1.0, msg("server.job.done", "Done")["message"])
    per = ", ".join(f"track {i + 1} {audio.SPEAKER.get(r) or r} {per_track.get(i, 0)}"
                    + (f" {_tl(i)['lang']}" if _tl(i)["lang"] else "") for i, r in speech)
    facts = {"segments": len(rows), "tracks": per, "words": n_words, "minutes": f"{speech_s / 60:.1f}", "model": model,
             "device": stt.engine.device, "speed": f"{duration * len(speech) / max(elapsed, 0.01):.0f}",
             "dropped": dropped}
    if vres:
        return msg("server.job.transcribe_done_voice", "{segments} segments ({tracks}), {words} words, {minutes} min "
                   "speech, {model} on {device} ({speed}x realtime, {dropped} junk dropped) | voice events {keyword} "
                   "keyword, {excitement} excitement", keyword=vres["keyword"], excitement=vres["excitement"],
                   **facts)["message"]
    return msg("server.job.transcribe_done", "{segments} segments ({tracks}), {words} words, {minutes} min speech, "
               "{model} on {device} ({speed}x realtime, {dropped} junk dropped)", **facts)["message"]


def _track_language(stt, wav, model: str, cfg, lp: dict, rec_id: int, idx: int) -> list:
    """[language, probability] of one speech track. A fixed language is taken as is (probability None); otherwise
    whisper's language detection on the track's speech, restricted to the short list when there is one. No speech
    to tell from / no detector: the list's first language, or None (= whisper decides while transcribing)."""
    if lp["mode"] == "fixed":
        return [lp["codes"][0], None]
    det = getattr(stt.engine, "detect_language", None)
    res = None
    if det is not None:
        try:
            res = det(wav, model=model, compute=cfg.stt.compute, allowed=lp["codes"] or None)
        except Exception as e:  # noqa: BLE001 - a failed detection falls back, it never costs the transcript
            logger.warning("[STT] language detection failed rec=%s track=%d err=%s", rec_id, idx,
                           str(e).encode("ascii", "replace").decode())
    if res and res.get("lang"):
        logger.info("[STT] language rec=%s track=%d lang=%s prob=%.2f", rec_id, idx, res["lang"], float(res["prob"]))
        return [res["lang"], float(res["prob"])]
    fallback = lp["codes"][0] if lp["codes"] else None
    logger.info("[STT] language rec=%s track=%d not detected (too little speech) -> %s", rec_id, idx, fallback or "whisper")
    return [fallback, None]


def _finish_transcribe(ctx, rows: list, voice: int | None, note: str | None, model: str | None = None,
                       excitement: list | None = None, tracks: dict | None = None,
                       secs: float | None = None, resumed: bool = False, language: str | None = None,
                       translated: int | None = None) -> dict | None:
    """Stage bookkeeping + the voice highlight events (keyword events from `rows`,
    excitement events computed from the wav by the caller). With no speech the recording's
    untouched voice events are cleared. Returns the voice-event counts (None if that failed)."""
    from core import highlights, transcripts
    if not rows:
        transcripts.replace_segments(ctx.db, ctx.rec["id"], [], model or ctx.cfg.stt.model)
    vres = None
    try:
        vres = highlights.build_and_write(ctx.db, ctx.cfg, ctx.rec["id"], rows, excitement or [])
    except Exception as e:  # noqa: BLE001 - a voice-event failure must not fail the transcript
        logger.warning("[Voice] voice events failed rec=%s err=%s", ctx.rec["id"], e, exc_info=True)
    extra = {"voice_events": {"keyword": vres["keyword"], "excitement": vres["excitement"]}} if vres else {}
    if tracks:
        extra["tracks"] = tracks
    # v: the per-track pass (mic + squad). "Transcribe all > outdated" redoes anything older
    extra["v"] = transcripts.TRANSCRIBE_VERSION
    if secs is not None:
        extra["secs"] = round(secs, 1)
    if resumed:
        extra["resumed"] = True                      # went on from a checkpoint (core/checkpoints.py)
    if language:
        extra["language"] = language                 # the speech language setting the run used
    if translated is not None:
        extra["translated"] = translated
    ctx.db.execute(f"UPDATE recordings SET {STAGE_SQL}, updated_utc=? WHERE id=?",
                   (*stage_params("transcribe", "done", n=len(rows), voice_track=voice, model=model, note=note, **extra),
                    time.time(), ctx.rec["id"]))
    fresh = ctx.db.one("SELECT * FROM recordings WHERE id=?", (ctx.rec["id"],))
    # a proxy built before the roles were known has the game track only: rebuild it with the voice mixed in
    try:
        from media import clips
        if fresh and clips.proxy_stale(ctx.db, fresh, ctx.cfg) and getattr(ctx, "queue", None) is not None:
            if ctx.queue.enqueue(ctx.rec["id"], "proxy") is not None:
                logger.info("[STT] proxy audio changed rec=%s -- proxy rebuild queued", ctx.rec["id"])
    except Exception as e:  # noqa: BLE001 - bookkeeping must never fail the transcript
        logger.warning("[STT] proxy staleness check failed rec=%s err=%s", ctx.rec["id"], e)
    ctx.bus.publish("transcript", action="done", rec_id=ctx.rec["id"], n=len(rows), note=note)
    if vres is not None:
        ctx.bus.publish("events", action="updated", rec_id=ctx.rec["id"])
    ctx.bus.publish("recording", action="updated", rec=rec_to_dict(fresh))
    return vres


def detect(ctx) -> str:
    """Kills / deaths / assists from the HUD: see vision/detect.py (imported lazily: OpenCV +
    onnxruntime are heavy and only the video lane needs them)."""
    from vision import detect as detect_mod
    return detect_mod.run(ctx)


def proxy(ctx) -> str:
    """A small browser-playable copy of the whole recording (see media/clips.py)."""
    from media import clips
    return clips.build_proxy(ctx)


def makeclip(ctx) -> str:
    """A trimmed copy of a moment into the clip pool (see media/makeclip.py)."""
    from media import makeclip as makeclip_mod
    return makeclip_mod.run(ctx)


def register_all(queue) -> None:
    queue.register("probe", probe)
    queue.register("transcribe", transcribe)
    queue.register("detect", detect)
    queue.register("proxy", proxy)
    queue.register("makeclip", makeclip)
