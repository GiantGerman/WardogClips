"""Stage handlers, registered on the JobQueue by ``register_all``: `probe`,
`transcribe`, `detect` and `proxy` (`download` is registered in server/app.py).

A handler gets a JobContext, reports progress, writes its results to the db and returns a
one-line summary for the job row. Raising marks the job failed (and never kills the lane).
"""

import json
import logging
import os
import time
from pathlib import Path

from core.library import rec_to_dict
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
        raise RuntimeError("file is not there any more")
    ctx.progress(0.1, "ffprobe")
    try:
        info = ffmpeg.probe(path)
        if not info.get("duration_s"):
            raise ffmpeg.ProbeError("no duration yet (still being written?)")
        ctx.progress(0.6, "keyframe interval")
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
    ctx.progress(1.0, "done")
    return (f"{info['duration_s']:.0f}s {info['width']}x{info['height']} {info['vcodec']} "
            f"{(info['fps'] or 0):.0f}fps {len(info['audio_tracks'])} audio keyint={ki}"
            + (f" -> queued {'+'.join(follow)}" if follow else ""))


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
        raise RuntimeError("speech-to-text is disabled (Settings > Speech-to-text)")
    if rec["status"] != "probed" or not rec.get("duration_s"):
        raise RuntimeError("probe the recording first")
    path, duration = rec["path"], float(rec["duration_s"])
    if not os.path.exists(path):
        ctx.db.execute("UPDATE recordings SET missing=1, updated_utc=? WHERE id=?", (time.time(), rec["id"]))
        raise RuntimeError("file is not there any more")
    if not stt.available():
        raise RuntimeError("faster-whisper is not installed - reinstall WardogClips")
    try:
        tracks = json.loads(rec.get("audio_tracks") or "[]")
    except json.JSONDecodeError:
        tracks = []
    if not tracks:
        _finish_transcribe(ctx, [], None, "no audio tracks")
        return "no audio tracks"

    # resume point (core/checkpoints.py): a transcribe cut short (app closed, stopped, crashed) keeps its wavs and the
    # lines so far, and the next run goes on from the end of the last segment whisper finished
    from core import checkpoints
    tag = f"rec{rec['id']}"
    lay = audio.obs_layout(cfg, rec)              # a stream VOD: one mixed track, no OBS names / overrides
    hot = ", ".join(x for x in (cfg.player.aliases + cfg.player.squad +
                                [h.strip() for h in cfg.stt.hotwords.split(";")]) if x)
    mic_override = cfg.audio.mic_track if rec.get("kind") != "vod" else -1
    fp = {"file": checkpoints.file_stamp(path), "duration": duration, "tracks": len(tracks), "model": cfg.stt.model,
          "compute": cfg.stt.compute, "hot": hot, "words": bool(cfg.stt.word_timestamps), "vad": bool(cfg.stt.vad),
          "lay": {k: lay[k] for k in ("voice", "game", "voip", "normalize_mic", "names") if k in lay},
          "mic": mic_override, "v": transcripts.TRANSCRIBE_VERSION,
          **({"batched": int(cfg.stt.batch_size)} if cfg.stt.batched else {}),
          "code": checkpoints.code_stamp("core/stages.py", "stt/whisper.py", "media/audio.py")}
    saved = checkpoints.load("transcribe", rec["id"], fp)
    if saved and not all(os.path.exists(f) for f in saved["files"]):
        logger.info("[STT] rec=%s checkpoint wavs are gone -- starting over", rec["id"])
        checkpoints.clear("transcribe", rec["id"], state=saved)
        saved = None
    rows, n_words, dropped = [], 0, 0
    whisper_s = job_s = 0.0                       # whisper / whole-stage wall time of earlier runs
    done_k, cut = 0, 0.0                          # tracks finished, and how far into track done_k whisper got
    keep_wavs = False
    if saved:
        speech = [tuple(x) for x in saved["speech"]]
        specs = [(i, Path(w), n) for i, w, n in saved["specs"]]
        raw_shout = Path(saved["raw_shout"]) if saved["raw_shout"] else None
        rows, n_words, dropped = saved["rows"], saved["n_words"], saved["dropped"]
        whisper_s, job_s, done_k, cut = saved["whisper_s"], saved["job_s"], saved["done_k"], saved["cut"]
        track_rows = {int(k): v for k, v in saved["track_rows"].items()}
        logger.info("[STT] rec=%s resuming at track %d %.1fs (%d lines kept)", rec["id"],
                    speech[min(done_k, len(speech) - 1)][0] + 1, cut, len(rows))
        ctx.progress(0.30 + 0.69 * (done_k + min(1.0, cut / duration)) / len(speech),
                     f"resuming at {int(cut) // 60}:{int(cut) % 60:02d}")
    else:
        # 1. which tracks carry speech: your mic, the squad (Discord), or an unnamed voice / mix track
        ctx.progress(0.02, "measuring audio tracks")
        measures = audio.measure_tracks(path, duration, len(tracks), paths.CACHE_DIR, tag)
        roles = audio.decide_roles(measures, lay["voice"], lay["game"], names=lay["names"],
                                   mic_override=mic_override, voip_override=lay["voip"])
        track_rows = {i: [role, measures[i]["speech_share"], measures[i]["rms_db"]] for i, role in roles.items()}
        speech = audio.speech_tracks(roles)
        logger.info("[STT] roles rec=%s %s", rec["id"], " ".join(f"t{i}={r}" for i, r in sorted(roles.items())))
        if not speech:
            _finish_transcribe(ctx, [], None, "no speech found on any track", secs=time.monotonic() - t_job)
            return "no speech found on any track"
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
            "rows": rows, "n_words": n_words, "dropped": dropped,
            "whisper_s": whisper_s + time.monotonic() - whisper_t0, "job_s": job_s + time.monotonic() - t_job,
            "done_k": done_k, "cut": cut})

    try:
        if not saved:
            audio.extract_tracks(path, specs, duration,
                                 on_progress=lambda f: ctx.progress(0.05 + 0.25 * f, f"extracting tracks {label}"))
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
                for seg in stt.engine.transcribe(wav_for[i], model=cfg.stt.model, compute=cfg.stt.compute, hotwords=hot,
                                                 word_timestamps=cfg.stt.word_timestamps, vad=cfg.stt.vad,
                                                 **({"offset_s": offset} if offset else {}),
                                                 **({"batched": True, "batch_size": int(cfg.stt.batch_size)}
                                                    if cfg.stt.batched else {})):
                    in_seg = True
                    if stt.is_junk(seg, hot):
                        dropped += 1
                    else:
                        for piece in stt.split_at_gaps(seg):
                            piece["track"], piece["speaker"] = i, speaker
                            rows.append(piece)
                            n_words += len(piece.get("words") or []) or len(piece["text"].split())
                    cut, in_seg = max(cut, float(seg["t1"])), False
                    frac = (k + min(1.0, seg["t1"] / duration)) / len(speech)
                    ctx.progress(0.30 + 0.69 * frac, f"{cfg.stt.model} track {i + 1}{' (' + speaker + ')' if speaker else ''} "
                                 f"{int(seg['t1']) // 60}:{int(seg['t1']) % 60:02d} / {int(duration) // 60}:{int(duration) % 60:02d}")
                    if ctx.stopping():
                        raise RuntimeError("stopped while transcribing")
                    if time.monotonic() >= next_save:
                        _checkpoint()
                        next_save = time.monotonic() + checkpoints.CHECKPOINT_EVERY_S
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
        ctx.progress(0.99, "voice highlights")
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
    transcripts.replace_segments(ctx.db, rec["id"], rows, cfg.stt.model)
    speech_s = sum(r["t1"] - r["t0"] for r in rows)
    vres = _finish_transcribe(ctx, rows, speech[0][0], None, model=cfg.stt.model, excitement=excitement,
                              tracks={str(i): {"role": r, "lines": per_track.get(i, 0)} for i, r in speech},
                              secs=job_s + time.monotonic() - t_job, resumed=bool(saved))
    checkpoints.clear("transcribe", rec["id"], keep_files=True)
    ctx.progress(1.0, "done")
    per = ", ".join(f"track {i + 1} {audio.SPEAKER.get(r) or r} {per_track.get(i, 0)}" for i, r in speech)
    return (f"{len(rows)} segments ({per}), {n_words} words, {speech_s / 60:.1f} min speech, "
            f"{cfg.stt.model} on {stt.engine.device} ({duration * len(speech) / max(elapsed, 0.01):.0f}x realtime, {dropped} junk dropped)"
            + (f" | voice events {vres['keyword']} keyword, {vres['excitement']} excitement" if vres else ""))


def _finish_transcribe(ctx, rows: list, voice: int | None, note: str | None, model: str | None = None,
                       excitement: list | None = None, tracks: dict | None = None,
                       secs: float | None = None, resumed: bool = False) -> dict | None:
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


def register_all(queue) -> None:
    queue.register("probe", probe)
    queue.register("transcribe", transcribe)
    queue.register("detect", detect)
    queue.register("proxy", proxy)
