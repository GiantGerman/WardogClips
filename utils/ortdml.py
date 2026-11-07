"""Load onnxruntime's DirectML build for GPU OCR.

The DirectML build (`onnxruntime-directml`) and the CPU build (`onnxruntime`, which faster-whisper / rapidocr name as
a dependency) install into the same `onnxruntime` package folder, so pip would put the CPU build back over it on the
next dependency install. The DirectML build therefore lives in its own folder, `venv\\ort-directml`
(`pip install --no-deps --target venv\\ort-directml onnxruntime-directml==1.24.4`), and
`activate()` puts that folder first on sys.path BEFORE anything imports onnxruntime (main.py's first lines). The
DirectML build also runs everything on the CPU, so whisper's Silero VAD keeps working. Tests never call it.
"""

import logging
import sys

from utils import paths

logger = logging.getLogger("wclips.ortdml")

DIRNAME = "ort-directml"
VERSION = "1.24.4"


def candidates() -> list:
    """Where the DirectML build may live, in order: a source run's venv, then the installed layout
    (<install root>/runtime/ort-directml; PROJECT_ROOT is <install root>/app there)."""
    return [paths.PROJECT_ROOT / "venv" / DIRNAME, paths.PROJECT_ROOT.parent / "runtime" / DIRNAME]


def folder():
    """The first candidate that holds onnxruntime/ (else the first, for the 'not installed' log line)."""
    cands = candidates()
    for d in cands:
        if (d / "onnxruntime").is_dir():
            return d
    return cands[0]


def activate() -> str:
    """Put the DirectML build first on sys.path. Returns what happened (for the startup log)."""
    d = folder()
    if not (d / "onnxruntime").is_dir():
        return f"DirectML build not installed ({d}) -- OCR on the CPU only"
    if "onnxruntime" in sys.modules:
        return "onnxruntime was already imported -- OCR on the CPU only"
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    return f"DirectML build from {d}"
