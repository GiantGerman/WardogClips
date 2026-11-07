"""Library list thumbnails: one small JPEG per recording, made once and cached under the data dir.

The frame is taken at 30 % of the recording (past menus / loading screens, before the end screen). The footage is
only read (one input-seek + one frame, ffmpeg -ss before -i), never written. The cache name carries the file's size and
mtime, so a re-recorded / replaced file gets a fresh thumbnail and stale ones are swept with it.

A library can hold hundreds of recordings on a slow HDD, and the page asks for every visible row at once: at most
MAX_PARALLEL frame grabs run at a time (the rest wait their turn), and a recording whose grab failed is not retried for
FAIL_RETRY_S so a broken file does not cost an ffmpeg start on every page refresh.
"""

import logging
import threading
import time
from pathlib import Path

from utils import paths

logger = logging.getLogger("wclips.thumbs")

WIDTH = 320                 # 2x the 160 px the list shows at most (sharp on high-DPI screens)
AT_FRACTION = 0.30
MAX_PARALLEL = 2
FAIL_RETRY_S = 600.0

_sem = threading.BoundedSemaphore(MAX_PARALLEL)
_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()
_failed: dict[str, float] = {}


def _key(rec: dict) -> str:
    return f"rec{int(rec['id'])}_{int(rec.get('size') or 0)}_{int(float(rec.get('mtime') or 0))}"


def thumb_file(rec: dict) -> Path:
    return paths.THUMBS_DIR / f"{_key(rec)}_w{WIDTH}.jpg"


def _lock_for(rec_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(rec_id, threading.Lock())


def get(rec: dict) -> Path | None:
    """The cached thumbnail of `rec`, made now if missing. None when the file cannot be read (missing, broken)."""
    f = thumb_file(rec)
    if f.exists():
        return f
    if rec.get("missing") or not rec.get("path"):
        return None
    key = _key(rec)
    t_fail = _failed.get(key)
    if t_fail and time.time() - t_fail < FAIL_RETRY_S:
        return None
    with _lock_for(int(rec["id"])):            # two requests for one row: the second one waits and reuses
        if f.exists():
            return f
        from vision import frames
        dur = float(rec.get("duration_s") or 0.0)
        t = dur * AT_FRACTION if dur > 0 else 5.0
        t0 = time.time()
        with _sem:
            try:
                data = frames.grab_frame(rec["path"], t, max_w=WIDTH, quality=5, timeout=30.0)
            except Exception as e:  # noqa: BLE001 - FrameError, OSError: no thumbnail, the row shows its placeholder
                _failed[key] = time.time()
                logger.info("[Thumbs] no thumbnail rec=%s reason=%s", rec.get("id"), str(e)[:120])
                return None
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(f)
        for old in f.parent.glob(f"rec{int(rec['id'])}_*_w{WIDTH}.jpg"):   # an older size / mtime of this recording
            if old != f:
                try:
                    old.unlink()
                except OSError:
                    pass
        logger.debug("[Thumbs] made rec=%s t=%.1f ms=%d bytes=%d", rec.get("id"), t, (time.time() - t0) * 1000, len(data))
        return f
