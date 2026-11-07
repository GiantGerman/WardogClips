"""Image files on data-dir paths that may hold any character.

cv2.imwrite / cv2.imread go through the C runtime's ANSI file API on Windows: a path with a character outside the
ANSI code page (a Windows user name with an accent or in Chinese -- and the data dir defaults to
%LOCALAPPDATA%\\WardogClips) makes imwrite return False and write nothing, and imread return None. Encode / decode
in memory and do the file I/O in Python, which uses the wide API.
"""

import logging
from pathlib import Path

logger = logging.getLogger("wclips.imgio")


def imwrite(path, img, params=None) -> bool:
    """cv2.imwrite for any path. False (and one WARN line) when the encode or the write failed."""
    import cv2
    p = Path(path)
    ext = p.suffix or ".png"
    try:
        ok, buf = cv2.imencode(ext, img, params or [])
    except cv2.error as e:
        logger.warning("[ImgIO] encode failed path=%s err=%s", p, e)
        return False
    if not ok:
        logger.warning("[ImgIO] encode failed path=%s", p)
        return False
    try:
        p.write_bytes(buf.tobytes())
        return True
    except OSError as e:
        logger.warning("[ImgIO] write failed path=%s err=%s", p, e)
        return False


def imread(path, flags=None):
    """cv2.imread for any path; None when the file is missing or not an image (as cv2.imread)."""
    import cv2
    import numpy as np
    try:
        data = np.fromfile(str(Path(path)), dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR if flags is None else flags)
