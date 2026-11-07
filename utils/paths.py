"""WardogClips path resolution -- standalone.

Per-user data in ``%LOCALAPPDATA%\\WardogClips`` for an installed copy AND a source
run; ``WARDOGCLIPS_DATA_DIR`` overrides the data dir (logged loudly by main.py). Installed layout:
``<install root>\\app`` = PROJECT_ROOT (holds BUILD_INFO.json),
``<install root>\\runtime`` = the embedded Python, ``<install root>\\vendor\\ffmpeg\\bin`` = ffmpeg / ffprobe.

The footage itself (settings.library.roots) is read in place and NEVER written to: every cache (proxies,
thumbnails, crops) lives under the data dir. Nothing here ever resolves a path under the game or Steam install
(ban safety).
"""

import os
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "WardogClips"

from utils.version import __version__ as APP_VERSION  # noqa: E402

# installed by the Setup (build/build_release.ps1 writes BUILD_INFO.json next to main.py)
BUILD_INFO_FILE = PROJECT_ROOT / "BUILD_INFO.json"
INSTALLED = BUILD_INFO_FILE.exists()
INSTALL_ROOT = PROJECT_ROOT.parent if INSTALLED else PROJECT_ROOT


def _local_appdata() -> Path:
    la = os.environ.get("LOCALAPPDATA")
    return Path(la) if la else Path.home() / "AppData" / "Local"


_DEFAULT_DATA_DIR = _local_appdata() / APP_NAME
DATA_DIR = Path(os.environ.get("WARDOGCLIPS_DATA_DIR") or _DEFAULT_DATA_DIR)
DATA_DIR_REASON = "env:WARDOGCLIPS_DATA_DIR" if os.environ.get("WARDOGCLIPS_DATA_DIR") else "default"

LOGS_DIR = DATA_DIR / "logs"
LOG_FILE = LOGS_DIR / "wardogclips.log"
SETTINGS_FILE = DATA_DIR / "settings.json"
DB_FILE = DATA_DIR / "clips.db"
PROXY_DIR = DATA_DIR / "proxies"        # browser-playable low-res H.264 per recording
THUMBS_DIR = DATA_DIR / "thumbs"        # per-event jpg
SPRITES_DIR = DATA_DIR / "sprites"      # scrubber strips
CROPS_DIR = DATA_DIR / "crops"          # evidence crops: ticker / kill feed
TEMPLATES_DIR = DATA_DIR / "templates"  # learned ticker word templates per HUD profile
OUT_DIR = DATA_DIR / "out"              # CSV / EDL exports
CACHE_DIR = DATA_DIR / "cache"          # temp wavs etc. (deleted after use)
PACKS_DIR = DATA_DIR / "packs"          # downloaded add-ons: cuda / deno / ytdlp (core/packs.py)
INSTANCE_FILE = DATA_DIR / "instance.json"  # {pid, port, version, started} while an instance serves
CLIPS_DIR = DATA_DIR / "clips"          # on-demand event clips for the player (size-capped, oldest deleted)

WEB_DIR = PROJECT_ROOT / "web"

# Shared vendor dir -- a sibling of the app folder, so binaries survive app
# updates. PATH ffmpeg is the normal case for a source run.
VENDOR_DIR = PROJECT_ROOT.parent / "vendor"


def _tool(name: str) -> str | None:
    vend = VENDOR_DIR / "ffmpeg" / "bin" / f"{name}.exe"
    if vend.exists():
        return str(vend)
    return shutil.which(name)


def ffmpeg_exe() -> str | None:
    return _tool("ffmpeg")


def ffprobe_exe() -> str | None:
    return _tool("ffprobe")


def python_exe() -> str:
    """The console python.exe for CHILD processes (python -m yt_dlp, the Resolve probe). Under pythonw.exe (the tray
    app) sys.executable is pythonw.exe -> use the python.exe next to it. Children still pass CREATE_NO_WINDOW, so
    nothing flashes."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        cand = exe.with_name("python.exe")
        if cand.exists():
            return str(cand)
    return sys.executable


def ensure_dirs() -> None:
    """Create every data directory WardogClips writes to. Idempotent."""
    for d in (DATA_DIR, LOGS_DIR, PROXY_DIR, THUMBS_DIR, SPRITES_DIR, CROPS_DIR, TEMPLATES_DIR, OUT_DIR, CACHE_DIR, CLIPS_DIR,
              PACKS_DIR):
        d.mkdir(parents=True, exist_ok=True)
