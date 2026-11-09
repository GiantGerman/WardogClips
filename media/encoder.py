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

AMD (AMF, ``h264_amf``) and Intel (Quick Sync, ``h264_qsv``): there is no driver-level session count to ask without
looking at processes, so ``auto`` never takes those engines on its own -- only when Settings > Hardware allows it
(``hardware.auto_gpu_encoder``), and then for jobs on a card of that brand. The default stays x264. Which card a job
runs on: utils/gpus.py.
"""

import logging
import os
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager

logger = logging.getLogger("wclips.encoder")

CHOICES = ("auto", "h264_nvenc", "hevc_nvenc", "h264_amf", "h264_qsv", "libx264")
NVENC = ("h264_nvenc", "hevc_nvenc")
HW_ENCODERS = NVENC + ("h264_amf", "hevc_amf", "h264_qsv", "hevc_qsv")    # a failed encode on these retries with x264
ENCODER_VENDOR = {"h264_nvenc": "nvidia", "hevc_nvenc": "nvidia", "h264_amf": "amd", "hevc_amf": "amd",
                  "h264_qsv": "intel", "hevc_qsv": "intel"}
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


def _auto_gpu(hw: dict) -> tuple[str, str] | None:
    """auto on this thread's card when it is not an NVIDIA card: (encoder, why); None = the NVIDIA logic below."""
    from utils import gpus
    if gpus.source() == "none":
        return None                                    # no card list at all: the NVIDIA-first behavior as before
    a = gpus.current()
    if a is None:
        return "libx264", "no graphics card in use (Settings > Hardware) -- x264 on the CPU"
    if a["vendor"] == "nvidia":
        return None
    enc = gpus.ENCODERS.get(a["vendor"])
    brand = gpus.VENDOR_NAMES.get(a["vendor"], "this")
    if not enc or not hw.get(enc):
        return "libx264", f"no video encoder for the {brand} card in this ffmpeg -- x264 on the CPU"
    hwcfg = getattr(gpus.settings(), "hardware", None)
    if not getattr(hwcfg, "auto_gpu_encoder", False):
        return "libx264", f"the {brand} card's encoder is off for auto (Settings > Hardware) -- x264 on the CPU"
    return enc, f"the {brand} card's encoder, allowed in Settings > Hardware"


def choose(setting: str, hw: dict | None = None) -> tuple[str, str]:
    """(ffmpeg encoder, why). An explicit setting is used as is (a GPU encoder falls back to x264 when this
    ffmpeg lacks it or no usable card of that brand is in the PC); auto = NVENC only while nothing else is encoding
    on it, an AMD / Intel encoder only when Settings > Hardware allows it."""
    global _last_reason
    if hw is None:
        from media import ffmpeg
        hw = ffmpeg.hw_info()
    setting = setting if setting in CHOICES else "auto"
    if setting != "auto":
        if setting in HW_ENCODERS and not hw.get(setting):
            return "libx264", f"{setting} not available in this ffmpeg"
        ven = ENCODER_VENDOR.get(setting)
        if ven:
            from utils import gpus
            if gpus.source() != "none" and not any(a["vendor"] == ven for a in gpus.usable()):
                return "libx264", f"no usable {gpus.VENDOR_NAMES[ven]} graphics card for {setting}"
        return setting, "set in Settings"
    other = _auto_gpu(hw)
    if other is not None:
        codec, why = other
        if why != _last_reason:
            logger.info("[Encoder] auto -> %s (%s)", codec, why)
            _last_reason = why
        return codec, why
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
