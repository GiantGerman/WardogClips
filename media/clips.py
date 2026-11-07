"""Player media: on-demand event clips and whole-recording proxies.

- `render_clip`: a short window of a recording (the event with handles either side) as a
  browser-playable H.264 / AAC mp4 under CLIPS_DIR. NVDEC decode, x264 or NVENC encode
  (media/encoder.py), a few seconds for a 20 s window of 3440x1440 footage. Cached by (recording, window, height); the folder is
  size-capped (oldest files deleted). The footage is only ever read.
- `build_proxy` (the `proxy` job stage): the whole recording at proxies.height for scrubbing
  in the "Full recording" view, with -progress for the job bar.

Audio: the game and voice tracks mixed (roles from the transcribe stage, settings override,
else OBS track 1 alone, which is the full mix on an untouched OBS setup).
"""

import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from media import encoder
from utils import paths

logger = logging.getLogger("wclips.clips")

_NO_WINDOW = 0x08000000
CLIP_HEIGHTS = (360, 540, 720, 1080, 1440)
CLIPS_CAP_BYTES = 4 * 1024 ** 3        # on-demand clips kept in the data dir before the oldest are deleted
MAX_WINDOW_S = 180.0                    # one clip render never spans more than this
_NAME_RE = re.compile(r"^rec\d+_\d+_\d+_\d+_a[\dn-]+\.mp4$")
# what the cache owns: today's names + the ones from before the audio tag (rec<id>_<t0ms>_<t1ms>_<h>.mp4);
# a proxy is rec<id>_<h>.mp4 and never matches
_CACHE_RE = re.compile(r"^rec\d+_\d+_\d+_\d+(?:_a[\dn-]+)?\.mp4$")

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_render_slots = threading.BoundedSemaphore(2)   # interactive renders at once (NVDEC/NVENC are shared with the jobs)


class ClipError(RuntimeError):
    pass


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or ""))


def _key_lock(key: str) -> threading.Lock:
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = _locks[key] = threading.Lock()
        return lk


MIX_ROLES = ("game", "mic", "voip", "voice")      # desktop / music / dup / silent stay out of the mix


def audio_plan(db, rec: dict, cfg) -> list[tuple[int, bool]]:
    """[(track idx, normalize)] to mix: game + your mic + the squad (Discord). Roles come from
    the transcribe stage's measurement; before that from the OBS track names; overrides win.
    The mic is normalized (a headset mic typically sits ~20 dB under the game and voice chat)."""
    from media import audio
    try:
        n = len(json.loads(rec.get("audio_tracks") or "[]"))
    except (TypeError, json.JSONDecodeError):
        n = 0
    if n == 0:
        return []
    lay = audio.obs_layout(cfg, rec)               # a stream VOD: no OBS names / overrides / mic lift
    roles = {r["idx"]: r["role"] for r in db.query("SELECT idx, role FROM tracks WHERE rec_id=?", (rec["id"],))
             if r["role"] not in ("unknown", None)}
    if not roles:
        # not transcribed yet: trust the OBS names (a silent track in the mix costs nothing)
        roles = {i: audio.name_role(nm) or "" for i, nm in enumerate(lay["names"][:n])}
    for idx, role in ((lay["game"], "game"), (lay["mic"], "mic"), (lay["voip"], "voip")):
        if 0 <= idx < n:
            roles = {i: r for i, r in roles.items() if r != role}
            roles[idx] = role
    order = {r: k for k, r in enumerate(MIX_ROLES)}
    picked = sorted(((i, r) for i, r in roles.items() if r in order and i < n), key=lambda x: (order[x[1]], x[0]))
    plan = [(i, lay["normalize_mic"] and r == "mic") for i, r in picked]
    return plan or [(0, False)]


def audio_tracks(db, rec: dict, cfg) -> list[int]:
    """The track indexes audio_plan mixes (normalized ones carry an 'n' in the cache tag)."""
    return [i for i, _n in audio_plan(db, rec, cfg)]


def _audio_args(tracks, bitrate: str = "192k") -> tuple[str, list[str]]:
    """(filter_complex fragment, output args) for the audio mix. tracks = [idx] or [(idx, normalize)]."""
    from media import audio
    plan = [(t, False) if isinstance(t, int) else (int(t[0]), bool(t[1])) for t in tracks]
    if not plan:
        return "", ["-an"]
    out = ["-map", "[a]", "-c:a", "aac", "-b:a", bitrate, "-ac", "2"]
    chains, labels = [], []
    for k, (i, norm) in enumerate(plan):
        if norm:
            chains.append(f"[0:a:{i}]{audio.MIC_NORM}[m{k}]")
            labels.append(f"[m{k}]")
        else:
            labels.append(f"[0:a:{i}]")
    if len(plan) == 1:
        return ";".join(chains + [f"{labels[0]}aresample=48000[a]"]), out
    mix = f"{''.join(labels)}amix=inputs={len(plan)}:duration=first:normalize=0,alimiter=limit=0.95,aresample=48000[a]"
    return ";".join(chains + [mix]), out


def _video_codec_args(codec: str, quality: int, bitrate_k: int | None = None) -> list[str]:
    from media import ffmpeg
    hw = ffmpeg.hw_info()
    if codec in ("h264_nvenc", "hevc_nvenc") and hw.get(codec):
        args = ["-c:v", codec, "-preset", "p4", "-tune", "hq", "-rc", "vbr"]
        if bitrate_k:
            args += ["-b:v", f"{bitrate_k}k", "-maxrate", f"{bitrate_k * 2}k", "-bufsize", f"{bitrate_k * 2}k"]
        else:
            args += ["-cq", str(quality), "-b:v", "0"]
        return args + ["-pix_fmt", "yuv420p"]
    args = ["-c:v", "libx264", "-preset", "veryfast"]
    args += ["-b:v", f"{bitrate_k}k"] if bitrate_k else ["-crf", str(quality)]
    return args + ["-pix_fmt", "yuv420p"]


def _cmd(path: str, out: str, t0: float | None, dur: float | None, height: int, tracks: list[int],
         codec: str, quality: int, bitrate_k: int | None, hwaccel: bool, progress: bool,
         extra_out: list[str] | None = None) -> list[str]:
    """extra_out: more outputs from the same read (the per-track stems, media/stems.py)."""
    exe = paths.ffmpeg_exe()
    if not exe:
        raise ClipError("ffmpeg not found on PATH")
    cmd = [exe, "-v", "error", "-nostdin", "-y"]
    if progress:
        cmd += ["-nostats", "-progress", "pipe:1"]
    if hwaccel:
        cmd += ["-hwaccel", "cuda"]
    if t0 is not None:
        cmd += ["-ss", f"{max(0.0, t0):.3f}"]
    if dur is not None:
        cmd += ["-t", f"{dur:.3f}"]          # an INPUT option: it limits every output, not just the first
    cmd += ["-i", path]
    afrag, aout = _audio_args(tracks)
    vfrag = f"[0:v:0]scale=-2:{int(height)}:flags=bicubic,format=yuv420p[v]"
    graph = vfrag + (";" + afrag if afrag else "")
    cmd += ["-filter_complex", graph, "-map", "[v]", *aout, *_video_codec_args(codec, quality, bitrate_k),
            "-g", "30", "-movflags", "+faststart", "-f", "mp4", out]
    return cmd + list(extra_out or [])


_BELOW_NORMAL = 0x00004000                 # BELOW_NORMAL_PRIORITY_CLASS: a CPU encode yields to the game


def _run(cmd: list[str], timeout: float, on_out_time=None, stop=None, low_priority: bool = False) -> None:
    flags = _NO_WINDOW | (_BELOW_NORMAL if low_priority and os.name == "nt" else 0)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", creationflags=flags)
    err_tail: list[str] = []

    def _drain():
        for line in proc.stderr:
            err_tail.append(line.rstrip())
            if len(err_tail) > 20:
                del err_tail[0]

    th = threading.Thread(target=_drain, daemon=True)
    th.start()
    t0 = time.monotonic()
    try:
        for line in proc.stdout:
            if time.monotonic() - t0 > timeout:
                proc.kill()
                raise ClipError(f"ffmpeg timed out after {timeout:.0f}s")
            if stop is not None and stop():
                proc.kill()
                raise ClipError("stopped")
            if on_out_time and line.startswith("out_time_us="):
                try:
                    on_out_time(int(line.split("=", 1)[1]) / 1e6)
                except ValueError:
                    pass
        proc.wait(timeout=max(5.0, timeout - (time.monotonic() - t0)))
    except subprocess.TimeoutExpired:
        proc.kill()
        raise ClipError(f"ffmpeg timed out after {timeout:.0f}s")
    finally:
        if proc.poll() is None:
            proc.kill()
        th.join(timeout=5)
    if proc.returncode != 0:
        raise ClipError(" | ".join(x for x in err_tail[-3:] if x) or f"ffmpeg exit {proc.returncode}")


def audio_tag(tracks) -> str:
    """The audio mix in a cache name: a clip rendered before the transcribe stage knew the
    track roles must not be served once they are known. tracks = [idx] or [(idx, normalize)];
    a normalized track carries an 'n' (a0-1n-2 = game, normalized mic, Discord)."""
    if not tracks:
        return "an"
    parts = []
    for t in tracks:
        i, norm = (t, False) if isinstance(t, int) else (int(t[0]), bool(t[1]))
        parts.append(f"{i}{'n' if norm else ''}")
    return "a" + "-".join(parts)


def clip_name(rec_id: int, t0: float, t1: float, height: int, tracks: list[int] | None = None) -> str:
    return (f"rec{int(rec_id)}_{int(round(t0 * 1000))}_{int(round(t1 * 1000))}_{int(height)}"
            f"_{audio_tag(tracks or [])}.mp4")


def render_clip(db, cfg, rec: dict, t0: float, t1: float, height: int = 720, timeout: float = 180.0) -> dict:
    """Render (or reuse) the window [t0, t1) of a recording. Returns {name, path, t0, t1, cached, render_s}."""
    if not rec.get("duration_s"):
        raise ClipError("probe the recording first")
    dur_total = float(rec["duration_s"])
    t0 = max(0.0, min(float(t0), dur_total - 0.5))
    t1 = max(t0 + 0.5, min(float(t1), dur_total, t0 + MAX_WINDOW_S))
    height = min(CLIP_HEIGHTS, key=lambda h: abs(h - int(height)))
    if rec.get("height"):
        height = min(height, int(rec["height"]))
    tracks = audio_plan(db, rec, cfg)
    name = clip_name(rec["id"], t0, t1, height, tracks)
    out = paths.CLIPS_DIR / name
    with _key_lock(name):
        if out.exists() and out.stat().st_size > 1024:
            os.utime(out)          # LRU: a reused clip is fresh
            return {"name": name, "path": str(out), "t0": t0, "t1": t1, "height": height, "cached": True, "render_s": 0.0}
        if not os.path.exists(rec["path"]):
            raise ClipError("the recording is not on disk (missing)")
        paths.CLIPS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".part.mp4")
        started = time.monotonic()
        with _render_slots:
            codec, why = encoder.choose(cfg.proxies.clip_codec)
            used, err = _encode(lambda c, hw: _cmd(rec["path"], str(tmp), t0, t1 - t0, height, tracks, c, 26, None, hw, True),
                                codec, timeout, what=f"clip rec={rec['id']} t={t0:.1f}")
            if used is None:
                _unlink(tmp)
                raise ClipError(err or "render failed")
        tmp.replace(out)
        secs = time.monotonic() - started
        logger.info("[Clips] rendered rec=%s %.1f-%.1fs h=%d audio=%s enc=%s %.1fs %.1f MB", rec["id"], t0, t1, height,
                    audio_tag(tracks), used, secs, out.stat().st_size / 1e6)
    prune()
    return {"name": name, "path": str(out), "t0": t0, "t1": t1, "height": height, "cached": False, "render_s": round(secs, 2),
            "encoder": used}


def _encode(make_cmd, codec: str, timeout: float, what: str, on_out_time=None, stop=None) -> tuple[str | None, str | None]:
    """Run an encode: NVDEC decode first, then CPU decode; an NVENC encode that still fails (no free
    session, driver hiccup, CUDA out of memory) is retried with x264, again NVDEC then CPU decode.
    x264 runs at below-normal priority. One overall deadline (``timeout``) covers every attempt, and
    a timeout is not retried. Returns (encoder that worked, None) or (None, last error); 'stopped'
    is re-raised at once."""
    attempts = [(codec, True), (codec, False)]
    if codec in encoder.NVENC:
        attempts += [("libx264", True), ("libx264", False)]
    deadline = time.monotonic() + timeout
    err = None
    for c, hw in attempts:
        left = deadline - time.monotonic()
        if left < 5.0:
            break
        try:
            with encoder.using(c):
                _run(make_cmd(c, hw), left, on_out_time=on_out_time, stop=stop, low_priority=(c == "libx264"))
            return c, None
        except ClipError as e:
            if str(e) == "stopped":
                raise
            err = str(e)
            logger.warning("[Clips] encode failed %s enc=%s hwdec=%s: %s", what, c, hw, e)
            if err.startswith("ffmpeg timed out"):
                break                                  # slow, not broken: another attempt would only double it
    return None, err or "encode timed out"


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def prune(cap_bytes: int = CLIPS_CAP_BYTES) -> int:
    """Delete the least recently used clips and window stem sets until the folder is under the
    cap. Whole-recording stems (written with a proxy) are kept like the proxy. Returns items removed."""
    import shutil
    from media import stems
    files = []
    for p in _cache_clips():
        try:
            st = p.stat()
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, p, "clip"))
    for d in _cache_stem_dirs():
        m = d / "manifest.json"
        try:
            mt = (m if m.exists() else d).stat().st_mtime
        except OSError:
            continue
        files.append((mt, stems.dir_size(d), d, "stems"))
    total = sum(f[1] for f in files)
    n = 0
    for mt, size, p, kind in sorted(files, key=lambda f: f[0]):
        if total <= cap_bytes:
            break
        if (p.name.endswith(".part.mp4") or p.name.endswith(".part")) and time.time() - mt < 600:
            continue
        if kind == "clip":
            # hold the clip's render lock: render_clip's cache-hit path checks + touches under it, so a
            # clip it is about to hand to the page can't vanish in between
            lk = _key_lock(p.name)
            if not lk.acquire(blocking=False):
                continue
            try:
                try:
                    if p.stat().st_mtime > mt + 0.5:
                        continue                  # served again since this pass looked: keep it
                except OSError:
                    continue                      # already gone
                _unlink(p)
            finally:
                lk.release()
            if p.exists():
                continue                          # open (being streamed): Windows refused, not removed
        else:
            # hold the set's lock while deleting it: render_window can't serve a dir that is going away
            lk = stems._lock(p.name)
            if not lk.acquire(blocking=False):
                continue
            try:
                m = p / "manifest.json"
                if m.exists() and m.stat().st_mtime > mt + 0.5:
                    continue                      # served again since this pass looked: keep it
                # rename first: Windows refuses to rename a dir while a file in it is open, so a set the
                # page is streaming stays whole instead of losing its manifest and the other tracks
                gone = p.with_name(p.name + ".del")
                try:
                    p.rename(gone)
                except OSError:
                    continue
                shutil.rmtree(gone, ignore_errors=True)
            finally:
                lk.release()
        total -= size
        n += 1
    return n


def _cache_clips():
    """Clip mp4s of the cache (and their .part renders) -- by name, so a proxies.dir pointed at the
    clips folder never has its proxies counted or deleted."""
    if not paths.CLIPS_DIR.is_dir():
        return []
    out = []
    for p in paths.CLIPS_DIR.glob("*.mp4"):
        base = p.name[:-9] + ".mp4" if p.name.endswith(".part.mp4") else p.name
        if _CACHE_RE.match(base):
            out.append(p)
    return out


def _cache_stem_dirs():
    """Window stem sets (not the whole-recording ones a proxy build writes). Leftover '.del' dirs from
    an interrupted delete are removed on the way."""
    import shutil
    from media import stems
    root = stems.stems_root()
    if not root.is_dir():
        return []
    out = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        if d.name.endswith(".del"):
            shutil.rmtree(d, ignore_errors=True)
            continue
        if "_full_" not in d.name:
            out.append(d)
    return out


def cache_usage() -> dict:
    """The on-demand cache: clip mp4s + window stem sets (not proxies, not whole-recording stems)."""
    from media import stems
    n, size = 0, 0
    for p in _cache_clips():
        try:
            size += p.stat().st_size
            n += 1
        except OSError:
            pass
    for d in _cache_stem_dirs():
        size += stems.dir_size(d)
        n += 1
    return {"items": n, "bytes": size, "cap_bytes": CLIPS_CAP_BYTES}


def clear_cache() -> dict:
    """Delete every cached clip and window stem set (Settings > Clip cache). Anything being rendered
    or streamed right now is skipped (Windows keeps open files) and goes with the next prune."""
    before = cache_usage()
    removed = prune(0)
    after = cache_usage()
    logger.info("[Clips] cache cleared removed=%d freed_mb=%.0f left=%d",
                removed, (before["bytes"] - after["bytes"]) / 1e6, after["items"])
    return {"removed": removed, "freed_bytes": max(0, before["bytes"] - after["bytes"]), "left": after["items"]}


def clip_window(ev: dict, rec: dict, cfg, handles_s: float = 5.0) -> dict:
    """In / out of an event's clip (user-set or the pre / post roll) and the render window around it."""
    dur = float(rec.get("duration_s") or 0) or 1e9
    t = float(ev["t_s"])
    cin = ev.get("clip_in")
    cout = ev.get("clip_out")
    cin = max(0.0, float(cin) if cin is not None else t - cfg.highlights.pre_roll_s)
    cout = min(dur, float(cout) if cout is not None else t + cfg.highlights.post_roll_s)
    if cout <= cin:
        cout = min(dur, cin + 1.0)
    w0 = max(0.0, cin - handles_s)
    w1 = min(dur, cout + handles_s)
    # snap the window to whole seconds so small in / out edits reuse the cached render
    w0 = float(int(w0))
    w1 = float(min(dur, int(w1) + 1)) if w1 < dur else dur
    return {"t": t, "in": round(cin, 3), "out": round(cout, 3), "w0": w0, "w1": w1}


# ---- whole-recording proxy (job stage) ----------------------------------------------------
def proxy_stale(db, rec: dict, cfg) -> bool:
    """A built proxy whose audio mix no longer matches the track roles (it was built before the
    transcribe stage found the voice track, or the settings override changed)."""
    try:
        st = json.loads(rec.get("stages") or "{}").get("proxy") or {}
    except (TypeError, json.JSONDecodeError):
        return False
    if st.get("state") != "done" or not rec.get("proxy_path") or "audio" not in st:
        return False        # no proxy, or one from before proxies recorded their mix: leave it
    return st.get("audio") != audio_tag(audio_plan(db, rec, cfg))


def proxy_path(rec: dict, cfg) -> Path:
    base = Path(cfg.proxies.dir) if cfg.proxies.dir else paths.PROXY_DIR
    return base / f"rec{rec['id']}_{int(cfg.proxies.height)}.mp4"


def build_proxy(ctx) -> str:
    from core.library import rec_to_dict
    from core.stages import STAGE_SQL, stage_params
    rec, cfg = ctx.rec, ctx.cfg
    if rec["status"] != "probed" or not rec.get("duration_s"):
        raise RuntimeError("probe the recording first")
    if not os.path.exists(rec["path"]):
        ctx.db.execute("UPDATE recordings SET missing=1, updated_utc=? WHERE id=?", (time.time(), rec["id"]))
        raise RuntimeError("file is not there any more")
    duration = float(rec["duration_s"])
    out = proxy_path(rec, cfg)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part.mp4")
    tracks = audio_plan(ctx.db, rec, cfg)
    # the whole-recording stems for the track panel come out of the same read of the file
    import shutil
    from media import stems
    info = stems.track_info(ctx.db, rec, cfg)
    skey = stems.new_full_key(ctx.db, rec, cfg, info)
    sdir = stems.stems_root() / skey
    spart = sdir.with_name(skey + ".part")
    shutil.rmtree(spart, ignore_errors=True)
    if info:
        spart.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()

    def on_t(t):
        ctx.progress(min(0.99, t / duration), f"proxy {int(t) // 60}:{int(t) % 60:02d} / {int(duration) // 60}:{int(duration) % 60:02d}")

    codec, why = encoder.choose(cfg.proxies.codec)
    ctx.progress(0.01, f"starting ({'x264 on the CPU' if codec == 'libx264' else codec}: {why})")
    try:
        used, err = _encode(lambda c, hw: _cmd(rec["path"], str(tmp), None, None, cfg.proxies.height, tracks, c, 26,
                                               cfg.proxies.bitrate_k, hw, True,
                                               extra_out=stems.outputs(spart, info) if info else None),
                            codec, timeout=max(600.0, duration * 4), what=f"proxy rec={rec['id']}",
                            on_out_time=on_t, stop=ctx.stopping)
    except ClipError as e:                               # stopped
        used, err = None, str(e)
    if used is None:
        _unlink(tmp)
        shutil.rmtree(spart, ignore_errors=True)
        raise RuntimeError(f"proxy failed: {err}")
    tmp.replace(out)
    stem_key = None
    if info:
        try:
            stems.finalize(spart, info, 0.0, duration, stems.BUCKETS_FULL)
            stems.install(spart, sdir)
            stem_key = skey
            stems.cleanup_full(rec["id"], keep=skey)    # sets for an older normalized-track key
        except Exception as e:  # noqa: BLE001 - the proxy itself is fine; the track panel just lacks stems
            logger.warning("[Clips] proxy stems failed rec=%s: %s", rec["id"], e)
            shutil.rmtree(spart, ignore_errors=True)
    secs = time.monotonic() - t0
    size = out.stat().st_size
    ctx.db.execute(f"UPDATE recordings SET proxy_path=?, {STAGE_SQL}, updated_utc=? WHERE id=?",
                   (str(out), *stage_params("proxy", "done", height=cfg.proxies.height, mb=round(size / 1e6, 1),
                                            audio=audio_tag(tracks), stems=stem_key, enc=used),
                    time.time(), rec["id"]))
    fresh = ctx.db.one("SELECT * FROM recordings WHERE id=?", (rec["id"],))
    ctx.bus.publish("recording", action="updated", rec=rec_to_dict(fresh))
    ctx.progress(1.0, "done")
    return (f"{cfg.proxies.height}p {size / 1e6:.0f} MB in {secs:.0f}s ({duration / max(secs, 0.01):.1f}x) "
            f"enc={used} audio={tracks}")
