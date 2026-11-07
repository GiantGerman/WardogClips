"""ffmpeg / ffprobe wrappers: probing, the keyframe interval and the hardware inventory.

Every call is a subprocess with a timeout; nothing here reads or writes anything but
the footage file it is given (read-only) and paths under the data dir.
"""

import functools
import json
import logging
import statistics
import subprocess
import time

from utils import paths

logger = logging.getLogger("wclips.ffmpeg")

_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: no console flash from the server process


class ProbeError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=_NO_WINDOW)


def _frac(s: str | None) -> float | None:
    if not s or s == "0/0":
        return None
    try:
        if "/" in s:
            a, b = s.split("/", 1)
            return float(a) / float(b) if float(b) else None
        return float(s)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def probe(path: str, timeout: float = 60.0) -> dict:
    """Container + stream facts for one file. Raises ProbeError when ffprobe is missing
    or the file is unreadable (a file OBS is still writing probes fine; the moov atom is
    written at the end only for plain mp4 -- that case returns no duration and is retried)."""
    exe = paths.ffprobe_exe()
    if not exe:
        raise ProbeError("ffprobe not found on PATH")
    t0 = time.monotonic()
    try:
        r = _run([exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path], timeout)
    except subprocess.TimeoutExpired:
        raise ProbeError(f"ffprobe timed out after {timeout:.0f}s")
    if r.returncode != 0:
        raise ProbeError((r.stderr or "ffprobe failed").strip()[-300:])
    try:
        info = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        raise ProbeError("ffprobe returned no JSON")
    fmt = info.get("format", {})
    video = None
    audio = []
    for s in info.get("streams", []):
        if s.get("codec_type") == "video" and video is None:
            video = s
        elif s.get("codec_type") == "audio":
            audio.append({
                "idx": len(audio),
                "codec": s.get("codec_name"),
                "sample_rate": int(s.get("sample_rate") or 0) or None,
                "channels": s.get("channels"),
            })
    if video is None:
        raise ProbeError("no video stream")
    dur = fmt.get("duration")
    out = {
        "duration_s": float(dur) if dur else None,
        "bitrate": int(fmt.get("bit_rate") or 0) or None,
        "width": video.get("width"),
        "height": video.get("height"),
        "vcodec": video.get("codec_name"),
        "pix_fmt": video.get("pix_fmt"),
        "fps": _frac(video.get("avg_frame_rate")) or _frac(video.get("r_frame_rate")),
        "audio_tracks": audio,
    }
    logger.debug("[Probe] %s %.2fs -> %sx%s %s %.3g fps %d audio", path, time.monotonic() - t0,
                 out["width"], out["height"], out["vcodec"], out["fps"] or 0, len(audio))
    return out


def keyint(path: str, span_s: float = 20.0, timeout: float = 60.0) -> float | None:
    """Median keyframe interval (s) over the first `span_s` seconds. Decides the cadence of
    the keyframe-only triage pass (OBS footage typically has one every 2 s)."""
    exe = paths.ffprobe_exe()
    if not exe:
        return None
    try:
        r = _run([exe, "-v", "error", "-skip_frame", "nokey", "-select_streams", "v:0",
                  "-show_entries", "frame=pts_time", "-of", "csv=p=0",
                  "-read_intervals", f"%+{span_s:.0f}", path], timeout)
    except subprocess.TimeoutExpired:
        return None
    pts = []
    for line in (r.stdout or "").splitlines():
        line = line.strip().rstrip(",")
        try:
            pts.append(float(line))
        except ValueError:
            continue
    if len(pts) < 3:
        return None
    diffs = [b - a for a, b in zip(pts, pts[1:]) if b > a]
    return round(statistics.median(diffs), 3) if diffs else None


@functools.lru_cache(maxsize=1)
def hw_info() -> dict:
    """What this ffmpeg can do on this machine (cached for the process lifetime)."""
    exe = paths.ffmpeg_exe()
    out = {"ffmpeg": exe, "ffprobe": paths.ffprobe_exe(), "version": None, "cuda": False,
           "hevc_cuvid": False, "h264_nvenc": False, "hevc_nvenc": False}
    if not exe:
        return out
    try:
        v = _run([exe, "-hide_banner", "-version"], 15)
        first = (v.stdout.splitlines() or [""])[0].replace("ffmpeg version ", "")
        out["version"] = first.split(" Copyright")[0].strip()[:40]
        hw = _run([exe, "-hide_banner", "-hwaccels"], 15).stdout
        out["cuda"] = "cuda" in hw.split()
        dec = _run([exe, "-hide_banner", "-decoders"], 15).stdout
        out["hevc_cuvid"] = " hevc_cuvid " in dec
        enc = _run([exe, "-hide_banner", "-encoders"], 15).stdout
        out["h264_nvenc"] = " h264_nvenc " in enc
        out["hevc_nvenc"] = " hevc_nvenc " in enc
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("[FFmpeg] hardware inventory failed", exc_info=True)
    return out
