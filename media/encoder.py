"""Which video encoder WardogClips uses for clips and proxies.

A consumer NVIDIA GPU (RTX class) has ONE NVENC engine. OBS typically records on it (e.g. 3440x1440 @ 60 fps,
NVENC H.264 or HEVC at 100-650 Mb/s), and its replay buffer keeps that session open the
whole time you play. A WardogClips render on the same engine (a clip, or a whole-recording proxy
at full speed) competes with OBS for it -> "encoding overloaded" / skipped frames in the recording.

The default (Settings > Encoding, proxies.codec / clip_codec) is ``libx264``: on a modern multi-core
CPU it is about as fast as NVENC here, because reading high-bitrate footage, not encoding, is the
bottleneck (and x264 files come out smaller) -- and it never touches the engine OBS records on.

``auto`` asks the driver how many NVENC sessions are open -- one GPU-level
number from ``nvidia-smi --query-gpu=encoder.stats.sessionCount``; no process is listed or opened
(ban safety: the game process is never enumerated) -- and when anything besides our own encodes
holds one (OBS), encodes on the CPU with x264 at below-normal priority instead (a 720p clip takes a few
seconds and the game keeps the CPU it asks for). With NVENC idle it uses
h264_nvenc as before. Decoding (NVDEC, ``-hwaccel cuda``) is a separate engine and unaffected.
"""

import logging
import os
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager

logger = logging.getLogger("wclips.encoder")

CHOICES = ("auto", "h264_nvenc", "hevc_nvenc", "libx264")
NVENC = ("h264_nvenc", "hevc_nvenc")
CACHE_S = 2.0
_NO_WINDOW = 0x08000000

_lock = threading.Lock()
_ours = 0                                  # NVENC encodes WardogClips itself is running right now
_cache: tuple[float, int | None] | None = None
_last_reason: str | None = None


def _smi() -> str | None:
    exe = shutil.which("nvidia-smi")
    if exe:
        return exe
    p = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")
    return p if os.path.isfile(p) else None


def nvenc_sessions(max_age: float = CACHE_S) -> int | None:
    """Open NVENC sessions on the GPU (all apps, ours included); None when the driver can't say."""
    global _cache
    with _lock:
        if _cache is not None and time.monotonic() - _cache[0] < max_age:
            return _cache[1]
    exe = _smi()
    n = None
    if exe:
        try:
            cp = subprocess.run([exe, "--query-gpu=encoder.stats.sessionCount", "--format=csv,noheader,nounits"],
                                capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
            vals = [int(x.strip()) for x in cp.stdout.splitlines() if x.strip().isdigit()]
            n = sum(vals) if vals else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            n = None
    with _lock:
        _cache = (time.monotonic(), n)
    return n


def choose(setting: str, hw: dict | None = None) -> tuple[str, str]:
    """(ffmpeg encoder, why). An explicit setting is used as is (NVENC falls back to x264 when this
    ffmpeg lacks it); auto = NVENC only while nothing else is encoding on it."""
    global _last_reason
    if hw is None:
        from media import ffmpeg
        hw = ffmpeg.hw_info()
    setting = setting if setting in CHOICES else "auto"
    if setting != "auto":
        if setting in NVENC and not hw.get(setting):
            return "libx264", f"{setting} not available in this ffmpeg"
        return setting, "set in Settings"
    if not hw.get("h264_nvenc"):
        return "libx264", "no NVENC in this ffmpeg"
    n = nvenc_sessions()
    with _lock:
        ours = _ours
    # NVENC only when the engine is completely free: our own session may not be open yet when this asks
    # (ffmpeg still opening the file), so "ours" can't be subtracted reliably -- n == 0 and ours == 0
    if n is None:
        codec, why = "libx264", "NVENC use unknown (nvidia-smi did not answer) -- CPU to stay clear of OBS"
    elif n > ours or (n > 0 and ours == 0):
        codec, why = "libx264", f"NVENC busy ({n} session{'s' if n != 1 else ''} open: OBS recording / replay buffer)"
    elif n > 0 or ours > 0:
        codec, why = "libx264", "NVENC in use by WardogClips' own encode -- the next one goes to the CPU"
    else:
        codec, why = "h264_nvenc", "NVENC idle"
    if why != _last_reason:
        logger.info("[Encoder] auto -> %s (%s)", codec, why)
        _last_reason = why
    return codec, why


@contextmanager
def using(codec: str):
    """Count our own NVENC encodes (an NVENC encode in flight keeps auto off the engine)."""
    global _ours, _cache
    nv = codec in NVENC
    if nv:
        with _lock:
            _ours += 1
    try:
        yield
    finally:
        if nv:
            with _lock:
                _ours -= 1
                _cache = None                  # the count cached during our encode included our own session


def status() -> dict:
    """For the page / Settings: what auto would pick right now and why."""
    codec, why = choose("auto")
    return {"auto_now": codec, "why": why, "nvenc_sessions": nvenc_sessions(), "ours": _ours}
