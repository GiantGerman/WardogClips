"""Make clip: write a trimmed copy of a moment to the clip pool, ready for any editor.

Two modes (Settings > Clip pool > mode, or per request):

- ``copy`` (default): a lossless stream copy of EVERY stream (``-map 0 -c copy``): the picture, every
  audio track separately (OBS: 6 stereo PCM tracks, never mixed down) and the stream metadata. Nothing is
  re-encoded, so it is fast and identical to the recording -- but a copied clip can only start on a
  keyframe: the start moves back to the keyframe at or before the in point (OBS writes one every ~2 s).
  The real in / out are measured afterwards and stored.
- ``exact``: starts on the exact frame. The picture is re-encoded with x264 at high quality, same
  resolution, frame rate and pixel format as the recording (CPU, below-normal priority: never the NVENC
  engine OBS records with); the audio tracks are still copied untouched.

Container: the recording's own (.mkv -> .mkv, .mov -> .mov), except an .mp4 holding PCM audio (what OBS
writes for lossless audio tracks) -> .mov: QuickTime is the container MP4 grew out of and every editor
reads PCM there, while PCM inside MP4 is a recent addition some editors refuse.

The pool folder (``clippool.dir``, default <Videos>\\WardogClips\\Clips) is made on first use and is never
scanned by the library, even when it lies inside a library root. Every clip made is listed in the
``made_clips`` table (source recording, event, requested and real in / out, mode, checks).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from fractions import Fraction
from pathlib import Path

from utils import paths

logger = logging.getLogger("wclips.makeclip")

MODES = ("copy", "exact")
MAX_LEN_S = 3600.0                    # one clip never spans more than an hour
_NO_WINDOW = 0x08000000
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
KIND_NAMES = {"kill": "Kill", "assist": "Assist", "death": "Death", "spotted": "Spotted", "voice": "Voice",
              "manual": "Mark", "revive": "Revive", "squad": "Squad"}

_DDL = """
CREATE TABLE IF NOT EXISTS made_clips (
    id            INTEGER PRIMARY KEY,
    rec_id        INTEGER,
    event_id      INTEGER,
    kind          TEXT NOT NULL DEFAULT 'clip',
    mode          TEXT NOT NULL,                     -- copy | exact
    path          TEXT NOT NULL,
    name          TEXT NOT NULL,
    req_in        REAL,                              -- what was asked for (seconds into the recording)
    req_out       REAL,
    in_s          REAL,                              -- what the file really holds (copy: from a keyframe)
    out_s         REAL,
    size          INTEGER,
    audio_tracks  INTEGER,
    checks        TEXT NOT NULL DEFAULT '{}',        -- json: the ffprobe comparison with the recording
    job_id        INTEGER,
    created_utc   REAL NOT NULL
)"""


class MakeClipError(RuntimeError):
    pass


def ensure_table(db) -> None:
    db.execute(_DDL)
    db.execute("CREATE INDEX IF NOT EXISTS idx_made_clips_rec ON made_clips(rec_id)")


# ---- the pool folder -------------------------------------------------------------------------
def default_pool_dir() -> Path:
    """<the Videos folder>\\WardogClips\\Clips (the same Videos folder the VOD import uses: the known folder unless
    it is synced by OneDrive, else %USERPROFILE%\\Videos)."""
    from media import vods
    return vods.default_vod_root().parent / "WardogClips" / "Clips"


def pool_dir(cfg) -> Path:
    d = str(getattr(getattr(cfg, "clippool", None), "dir", "") or "").strip()
    return Path(d) if d else default_pool_dir()


def _norm(p) -> str:
    return os.path.normcase(os.path.abspath(str(p))).rstrip("\\/")


def excluded_dirs(cfg) -> list[str]:
    """Folders the library walk must skip (normalized): the clip pool."""
    try:
        return [_norm(pool_dir(cfg))]
    except Exception:  # noqa: BLE001 - a broken setting must never stop a library scan
        logger.warning("[MakeClip] could not work out the clip folder", exc_info=True)
        return []


def is_under(path, dirs: list[str]) -> bool:
    p = _norm(path)
    return any(p == d or p.startswith(d + os.sep) for d in dirs)


# ---- naming ----------------------------------------------------------------------------------
def container_for(src_path: str, audio_codecs: list[str]) -> str:
    """Output extension: the recording's own container, but PCM audio never goes into .mp4 (-> .mov)."""
    ext = Path(src_path).suffix.lower()
    pcm = any(str(c or "").startswith("pcm_") for c in audio_codecs)
    if ext in (".mkv", ".mov"):
        return ext
    if ext == ".mp4":
        return ".mov" if pcm else ".mp4"
    return ".mkv"                       # anything else: Matroska holds every codec


def rec_start(rec: dict) -> float | None:
    """Wall-clock start of a recording: a replay's file name is when the buffer was SAVED (its end)."""
    nu = rec.get("name_utc")
    if not nu:
        return None
    if rec.get("kind") == "replay" and rec.get("duration_s"):
        return float(nu) - float(rec["duration_s"])
    return float(nu)


def clip_filename(rec: dict, kind: str | None, t_in: float, ext: str) -> str:
    """Kill_<date>_<time>_<recording name>.mov: what, when (local time of the in point), from which recording."""
    label = KIND_NAMES.get(kind or "", (kind or "clip").capitalize() if kind else "Clip")
    stem = _BAD_CHARS.sub("_", Path(rec.get("name") or f"rec{rec.get('id')}").stem).strip(" ._") or f"rec{rec.get('id')}"
    start = rec_start(rec)
    if start is not None:
        when = datetime.fromtimestamp(start + max(0.0, t_in)).strftime("%Y-%m-%d_%H-%M-%S")
        name = f"{label}_{when}_{stem}"
    else:
        t = int(max(0.0, t_in))
        name = f"{label}_{stem}_{t // 3600:02d}h{t % 3600 // 60:02d}m{t % 60:02d}s"
    return _BAD_CHARS.sub("_", name)[:180] + ext


def unique_path(folder: Path, name: str) -> Path:
    p = folder / name
    stem, ext = p.stem, p.suffix
    n = 2
    while p.exists() or p.with_name(p.stem + ".part" + ext).exists():
        p = folder / f"{stem}_{n}{ext}"
        n += 1
    return p


_NAME_LOCK = threading.Lock()


def reserve_path(folder: Path, name: str) -> tuple[Path, Path]:
    """A free final name and its .part file, created empty right away: two clips of the same moment made at the
    same time (copy and frame-exact, or several io workers) never pick the same name."""
    with _NAME_LOCK:
        for _ in range(1000):
            final = unique_path(folder, name)
            tmp = final.with_name(final.stem + ".part" + final.suffix)
            try:
                with open(tmp, "xb"):
                    pass
            except FileExistsError:
                continue
            return final, tmp
    raise MakeClipError("no free file name in the clip folder")


# ---- ffprobe helpers (module level, so they can be swapped out) --------------------------------
def _ffprobe_json(args: list[str], timeout: float = 60.0) -> dict:
    exe = paths.ffprobe_exe()
    if not exe:
        raise MakeClipError("ffprobe not found")
    try:
        r = subprocess.run([exe, "-v", "error", "-of", "json", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise MakeClipError(f"ffprobe timed out after {timeout:.0f}s")
    if r.returncode != 0:
        raise MakeClipError((r.stderr or "ffprobe failed").strip()[-300:])
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        raise MakeClipError("ffprobe returned no JSON")


def probe_streams(path: str) -> dict:
    """Every stream with its tags + the format (for the comparison and the stream titles)."""
    return _ffprobe_json(["-show_format", "-show_streams", path])


def keyframe_before(path: str, t: float, keyint: float | None = None) -> Fraction | None:
    """Time (seconds from the file's start, exact) of the last video keyframe at or before t. Reads only the
    packet headers of a few seconds before t (no decoding)."""
    look = max(6.0, 3.0 * float(keyint or 2.0))
    for start in (max(0.0, t - look), 0.0):
        info = _ffprobe_json(["-select_streams", "v:0", "-read_intervals", f"{start:.3f}%{t + 0.05:.3f}",
                              "-show_entries", "packet=pts,flags:stream=time_base:format=start_time", path])
        try:
            tb = Fraction(((info.get("streams") or [{}])[0]).get("time_base") or "1/1000")
            f0 = Fraction(str((info.get("format") or {}).get("start_time") or "0"))
        except (ValueError, ZeroDivisionError):
            return None
        best = None
        for pk in info.get("packets") or []:
            if "K" not in str(pk.get("flags") or "") or pk.get("pts") in (None, "N/A"):
                continue
            ts = Fraction(int(pk["pts"])) * tb - f0
            if ts <= Fraction(t).limit_denominator(1_000_000) + Fraction(1, 1_000_000) and (best is None or ts > best):
                best = ts
        if best is not None:
            return max(Fraction(0), best)
        if start == 0.0:
            break
    return None


def _ceil_us(x: Fraction) -> str:
    """Seconds as ffmpeg's -ss text, rounded UP to the microsecond: the seek lands on this keyframe, never the one before."""
    return f"{math.ceil(x * 1_000_000) / 1_000_000:.6f}"


def _run_ffmpeg(cmd: list[str], timeout: float, on_out_time=None, stop=None) -> None:
    from media import clips
    try:
        clips._run(cmd, timeout, on_out_time=on_out_time, stop=stop, low_priority=True)
    except clips.ClipError as e:
        raise MakeClipError(str(e))


# ---- the comparison ---------------------------------------------------------------------------
def _video(info: dict) -> dict:
    for s in info.get("streams") or []:
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic"):
            return s
    return {}


def _audio(info: dict) -> list[dict]:
    return [s for s in info.get("streams") or [] if s.get("codec_type") == "audio"]


def compare(src: dict, out: dict, mode: str) -> dict:
    """The output against its recording: same picture (size, frame rate, pixel format; copy: same codec and
    profile) and the same audio tracks (count, codec, rate, channels)."""
    sv, ov = _video(src), _video(out)
    problems = []
    keys = ["width", "height", "pix_fmt", "r_frame_rate"] + (["codec_name", "profile"] if mode == "copy" else [])
    for k in keys:
        if sv.get(k) != ov.get(k):
            problems.append(f"video {k} {sv.get(k)} -> {ov.get(k)}")
    sa, oa = _audio(src), _audio(out)
    if len(sa) != len(oa):
        problems.append(f"audio tracks {len(sa)} -> {len(oa)}")
    for i, (a, b) in enumerate(zip(sa, oa)):
        for k in ("codec_name", "sample_rate", "channels"):
            if a.get(k) != b.get(k):
                problems.append(f"audio {i + 1} {k} {a.get(k)} -> {b.get(k)}")
    return {"ok": not problems, "problems": problems,
            "video": {k: ov.get(k) for k in ("codec_name", "width", "height", "pix_fmt", "r_frame_rate", "profile")},
            "audio_tracks": len(oa)}


def _duration(info: dict) -> float | None:
    v = _video(info)
    for d in (v.get("duration"), (info.get("format") or {}).get("duration")):
        try:
            if d not in (None, "N/A"):
                return float(d)
        except (TypeError, ValueError):
            continue
    return None


# ---- the command ------------------------------------------------------------------------------
def build_cmd(src: str, out: str, ss: str, dur: float, mode: str, src_info: dict, track_names: list[str],
              crf: int = 16, preset: str = "fast") -> list[str]:
    exe = paths.ffmpeg_exe() or "ffmpeg"
    cmd = [exe, "-hide_banner", "-nostdin", "-y", "-ss", ss, "-i", src, "-t", f"{dur:.6f}"]
    if mode == "copy":
        cmd += ["-map", "0", "-c", "copy"]
    else:
        v = _video(src_info)
        cmd += ["-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", preset, "-crf", str(int(crf)),
                "-fps_mode", "passthrough", "-c:a", "copy"]
        if v.get("pix_fmt"):
            cmd += ["-pix_fmt", str(v["pix_fmt"])]
    cmd += ["-map_metadata", "0"]
    # OBS writes no track titles: name each audio track after the OBS track (Game, Microphone, Discord, ...)
    # so an editor shows what it is. A title the file already has stays.
    for i, a in enumerate(_audio(src_info)):
        tags = {str(k).lower(): v for k, v in (a.get("tags") or {}).items()}
        if not (tags.get("title") or tags.get("name")) and i < len(track_names) and str(track_names[i]).strip():
            cmd += [f"-metadata:s:a:{i}", f"title={str(track_names[i]).strip()}"]
    cmd += ["-nostats", "-progress", "pipe:1", out]
    return cmd


# ---- requests -----------------------------------------------------------------------------------
def plan(db, cfg, rec: dict, ev: dict | None, t_in, t_out, mode: str | None) -> dict:
    """Validate a request -> the job arg. In / out default to the event's clip (its own in / out, else the
    pre / post roll)."""
    if rec.get("status") != "probed" or not rec.get("duration_s"):
        raise MakeClipError("probe the recording first")
    dur = float(rec["duration_s"])
    if ev is not None and (t_in is None or t_out is None):
        from media import clips
        w = clips.clip_window(ev, rec, cfg, handles_s=0.0)
        t_in = w["in"] if t_in is None else t_in
        t_out = w["out"] if t_out is None else t_out
    try:
        t_in, t_out = float(t_in), float(t_out)
    except (TypeError, ValueError):
        raise MakeClipError("in and out are needed (seconds into the recording)")
    t_in, t_out = max(0.0, t_in), min(dur, t_out)
    if t_out - t_in < 0.1:
        raise MakeClipError("the out point must be after the in point")
    if t_out - t_in > MAX_LEN_S:
        raise MakeClipError("a clip can be at most one hour long")
    mode = mode or cfg.clippool.mode
    if mode not in MODES:
        raise MakeClipError(f"mode must be one of {', '.join(MODES)}")
    return {"in": round(t_in, 3), "out": round(t_out, 3), "mode": mode,
            "event_id": ev["id"] if ev else None, "kind": (ev or {}).get("kind") or "clip"}


def list_clips(db, limit: int = 50, rec_id: int | None = None) -> list[dict]:
    ensure_table(db)
    where, params = "", []
    if rec_id is not None:
        where, params = "WHERE rec_id=?", [rec_id]
    rows = db.query(f"SELECT * FROM made_clips {where} ORDER BY id DESC LIMIT ?", (*params, max(1, min(int(limit), 500))))
    return [_row(r) for r in rows]


def _row(r: dict) -> dict:
    out = dict(r)
    try:
        out["checks"] = json.loads(out.get("checks") or "{}")
    except json.JSONDecodeError:
        out["checks"] = {}
    out["exists"] = bool(out.get("path")) and os.path.isfile(out["path"])
    return out


def reveal(path: str | None, folder: Path) -> None:
    """Explorer on the pool folder (made if missing), with the clip selected when there is one."""
    folder.mkdir(parents=True, exist_ok=True)
    if path and os.path.isfile(path):
        subprocess.Popen(["explorer.exe", "/select,", os.path.normpath(path)])
    else:
        subprocess.Popen(["explorer.exe", os.path.normpath(str(folder))])


# ---- the job stage ------------------------------------------------------------------------------
def run(ctx) -> str:
    """Stage `makeclip` (io lane): jobs.arg = plan()'s JSON."""
    try:
        return _run(ctx)
    except Exception as e:
        if not ctx.stopping():
            from core.messages import text_i18n         # late: keeps this module free of the server catalogs
            ctx.bus.publish("makeclip", action="failed", rec_id=ctx.rec["id"] if ctx.rec else None,
                            job_id=ctx.job["id"], error=str(e)[:300], error_i18n=text_i18n(str(e)[:300]))
        raise


def _run(ctx) -> str:
    db, cfg, rec = ctx.db, ctx.cfg, ctx.rec
    ensure_table(db)
    try:
        arg = json.loads(ctx.job.get("arg") or "{}")
    except json.JSONDecodeError:
        raise MakeClipError("bad job argument")
    t_in, t_out, mode = float(arg["in"]), float(arg["out"]), arg.get("mode") or "copy"
    if mode not in MODES:
        raise MakeClipError(f"unknown mode {mode}")
    src = rec["path"]
    if not os.path.exists(src):
        db.execute("UPDATE recordings SET missing=1, updated_utc=? WHERE id=?", (time.time(), rec["id"]))
        raise MakeClipError("the recording is not there any more")
    ctx.progress(0.01, "reading the recording")
    src_info = probe_streams(src)
    ext = container_for(src, [a.get("codec_name") for a in _audio(src_info)])
    folder = pool_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    if mode == "copy":
        kf = keyframe_before(src, t_in, rec.get("keyint_s"))
        if kf is None:
            raise MakeClipError("no keyframe found before the in point")
        ss, start = _ceil_us(kf), float(kf)
    else:
        ss, start = f"{t_in:.6f}", t_in
    length = t_out - start
    names = [] if rec.get("kind") == "vod" else list(cfg.audio.track_names)
    final, tmp = reserve_path(folder, clip_filename(rec, arg.get("kind"), t_in, ext))
    cmd = build_cmd(src, str(tmp), ss, length, mode, src_info, names, crf=cfg.clippool.crf, preset=cfg.clippool.preset)
    logger.info("[MakeClip] start rec=%s mode=%s in=%.3f out=%.3f from=%s len=%.3f out=%s", rec["id"], mode, t_in,
                t_out, ss, length, final.name)

    def on_t(t):
        ctx.progress(min(0.98, 0.02 + 0.96 * t / max(length, 0.01)), f"{'copying' if mode == 'copy' else 'encoding'} {t:.0f} / {length:.0f} s")

    t0 = time.monotonic()
    try:
        _run_ffmpeg(cmd, timeout=max(300.0, length * (3 if mode == "copy" else 30)), on_out_time=on_t, stop=ctx.stopping)
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise MakeClipError("ffmpeg wrote no file")
        ctx.progress(0.99, "checking the clip")
        out_info = probe_streams(str(tmp))
        tmp.replace(final)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    secs = time.monotonic() - t0
    checks = compare(src_info, out_info, mode)
    real_len = _duration(out_info) or length
    in_s, out_s = round(start, 3), round(start + real_len, 3)
    checks["seek"] = ss
    size = final.stat().st_size
    cid = db.execute(
        "INSERT INTO made_clips (rec_id, event_id, kind, mode, path, name, req_in, req_out, in_s, out_s, size, audio_tracks,"
        " checks, job_id, created_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec["id"], arg.get("event_id"), arg.get("kind") or "clip", mode, str(final), final.name, t_in, t_out, in_s, out_s,
         size, checks["audio_tracks"], json.dumps(checks), ctx.job["id"], time.time()))
    row = _row(db.one("SELECT * FROM made_clips WHERE id=?", (cid,)))
    if not checks["ok"]:
        logger.warning("[MakeClip] clip differs from the recording id=%s problems=%s", cid, "; ".join(checks["problems"]))
    logger.info("[MakeClip] done id=%s rec=%s mode=%s in=%.3f out=%.3f size_mb=%.1f secs=%.1f tracks=%d ok=%s",
                cid, rec["id"], mode, in_s, out_s, size / 1e6, secs, checks["audio_tracks"], checks["ok"])
    ctx.bus.publish("makeclip", action="done", clip=row)
    ctx.progress(1.0, "done")
    early = t_in - in_s
    return (f"{final.name} {real_len:.1f}s {size / 1e6:.0f} MB {mode} in {secs:.1f}s"
            + (f", starts {early:.2f}s early (keyframe)" if mode == "copy" and early > 0.0005 else "")
            + ("" if checks["ok"] else " CHECK: " + "; ".join(checks["problems"])))[:200]
