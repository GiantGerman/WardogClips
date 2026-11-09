"""Add-on packs: the big optional parts the installer leaves out, downloaded from inside the app.

- cuda   = NVIDIA's cuBLAS / cuDNN / NVRTC DLLs from their PyPI wheels (GPU speech-to-text, ~1.3 GB)
- deno   = the deno.exe JS runtime yt-dlp needs for YouTube (when node is not on PATH)
- ytdlp  = a newer yt-dlp (+ yt-dlp-ejs) than the bundled one, put first on the yt-dlp child's PYTHONPATH
- model:<name> = a faster-whisper model into the Hugging Face cache (where stt/whisper.is_cached looks), fetched file by
  file by OUR downloader (not huggingface_hub.snapshot_download, see _install_model) and counted as installed only when
  every file of the repo listing is there at its listed size (`snapshot_complete`)

Everything lands in ``paths.PACKS_DIR`` (per-user data, survives reinstall; never the install dir). A pack counts as
installed only when ``packs/<id>/PACK.json`` exists -- it is written LAST, after the files were extracted to
``<id>.tmp`` and renamed into place, so a crash half-way never leaves a pack that looks installed.

Downloads are user-initiated only (the wizard / Settings) and only from the fixed hosts in ALLOWED_HOSTS (redirects
are followed by hand so every hop is checked). Every file is streamed to ``packs/.dl/<file>.part`` with a Range
resume and a sha256 check (PyPI's `digests`, the pinned deno digest). One install at a time per pack id, several
packs side by side. Nothing ever raises out of a worker thread: the state becomes failed with an ASCII error.

Cross-process safety: an install holds ``packs/.locks/<id>.lock`` (an OS byte lock, freed by the
OS if the process dies) for its whole run. ``sweep_leftovers`` (startup) only touches an id whose lock it can take, so
a second WardogClips process (or a headless scan) starting on the same data dir never deletes a running install's ``<id>.tmp``; a second
process asked to install the same id refuses. ``remove`` refuses while that id installs (here or in another process),
while a transcription holds the speech engine, and for the model the settings use.
"""

import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from core.messages import MsgError, annotate, text_i18n
from utils import paths
from utils.i18n import msg

logger = logging.getLogger("wclips.packs")

PACK_IDS = ("cuda", "deno", "ytdlp")
MODEL_PREFIX = "model:"

# a known-good set (ctranslate2 4.x wants CUDA 12 + cuDNN 9)
CUDA_WHEELS = (("nvidia-cublas-cu12", "12.9.2.10"), ("nvidia-cudnn-cu12", "9.27.0.42"),
               ("nvidia-cuda-nvrtc-cu12", "12.9.86"))
CUDA_SUBDIRS = ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime")

# deno: pinned release (asset size + sha256 from GitHub's release API)
DENO_VERSION = "2.9.8"
DENO_URL = f"https://github.com/denoland/deno/releases/download/v{DENO_VERSION}/deno-x86_64-pc-windows-msvc.zip"
DENO_SIZE = 42_917_472
DENO_SHA256 = "cba84b26e0df5ae7f74fc66b86a95b866872a86292ebd84fa1e959927edd93bf"

PYPI_JSON = "https://pypi.org/pypi/{name}/json"
PYPI_JSON_VER = "https://pypi.org/pypi/{name}/{ver}/json"

ALLOWED_HOSTS = {"pypi.org", "files.pythonhosted.org", "github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com", "huggingface.co"}
# Hugging Face's file CDN moves between names (cdn-lfs.hf.co, cas-bridge.xethub.hf.co, us.aws.cdn.hf.co, ...):
# any name under hf.co / huggingface.co. Every file is still sha256 / git-sha1 checked.
ALLOWED_HOST_SUFFIXES = (".hf.co", ".huggingface.co")
HF_API_REV = "https://huggingface.co/api/models/{repo}/revision/main?blobs=true"
HF_RESOLVE = "https://huggingface.co/{repo}/resolve/{rev}/{file}"

# the files faster-whisper's own download_model() fetches (no README / .gitattributes / onnx extras)
MODEL_ALLOW = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]
MODEL_SIZES_MB = {"tiny.en": 75, "tiny": 75, "small.en": 480, "small": 480, "medium.en": 1500, "medium": 1500,
                  "large-v3": 3100, "large-v3-turbo": 1600}
MODEL_REPOS_FALLBACK = {"large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo"}

# Plain-language copy: the title is what a stranger reads in Settings > Add-ons, the wizard's
# progress list and the status bar; desc = one plain sentence, then the technical one (Settings only shows desc).
# English text from msg(): the catalog keys are server.pack.<id>.title / .desc (status() adds <field>_i18n).
PACK_INFO = {
    "cuda": {"title": msg("server.pack.cuda.title", "Speech engine for your graphics card")["message"],
             "size_mb": 1310.0,
             "desc": msg("server.pack.cuda.desc",
                         "Turns speech into text on an NVIDIA graphics card, many times faster than on the processor. "
                         "NVIDIA CUDA libraries (cuBLAS, cuDNN) from NVIDIA's own PyPI packages.")["message"]},
    "deno": {"title": msg("server.pack.deno.title", "YouTube helper")["message"], "size_mb": 41.0,
             "desc": msg("server.pack.deno.desc",
                         "Lets WardogClips read YouTube pages when it downloads stream recordings. "
                         "The Deno JavaScript runtime for yt-dlp; not needed when Node.js is installed.")["message"]},
    "ytdlp": {"title": msg("server.pack.ytdlp.title", "VOD downloader update")["message"], "size_mb": 3.0,
              "desc": msg("server.pack.ytdlp.desc",
                          "A newer version of the part that downloads stream recordings, for when a site changes and "
                          "downloads stop working. The latest yt-dlp from PyPI, used instead of the built-in "
                          "one.")["message"]},
}

# speech models: (title, plain sentence) as msg() objects; the description adds the technical sentence
# (server.pack.model_desc). Keys: server.pack.model.<name with . and - as _>.title / .plain
MODEL_INFO = {
    "tiny.en": (msg("server.pack.model.tiny_en.title", "Light speech model"),
                msg("server.pack.model.tiny_en.plain", "Fastest and smallest, makes more mistakes. English.")),
    "small.en": (msg("server.pack.model.small_en.title", "Standard speech model"),
                 msg("server.pack.model.small_en.plain", "Good accuracy, quick enough on the processor. English.")),
    "medium.en": (msg("server.pack.model.medium_en.title", "Accurate speech model"),
                  msg("server.pack.model.medium_en.plain", "Best for English; meant for an NVIDIA graphics card.")),
    "tiny": (msg("server.pack.model.tiny.title", "Light speech model, many languages"),
             msg("server.pack.model.tiny.plain", "Fastest and smallest, makes more mistakes. Many languages.")),
    "small": (msg("server.pack.model.small.title", "Standard speech model, many languages"),
              msg("server.pack.model.small.plain", "Good accuracy, quick enough on the processor. Many languages.")),
    "medium": (msg("server.pack.model.medium.title", "Accurate speech model, many languages"),
               msg("server.pack.model.medium.plain", "Accurate in many languages; meant for an NVIDIA graphics card.")),
    "large-v3": (msg("server.pack.model.large_v3.title", "Multilingual speech model"),
                 msg("server.pack.model.large_v3.plain",
                     "Most accurate and understands many languages; slow without an NVIDIA graphics card.")),
    "large-v3-turbo": (msg("server.pack.model.large_v3_turbo.title", "Fast multilingual speech model"),
                       msg("server.pack.model.large_v3_turbo.plain",
                           "Nearly as accurate as the multilingual model, faster.")),
}

CHUNK = 1 << 20
CONNECT_TIMEOUT = 15
READ_TIMEOUT = 30            # per chunk read
PUBLISH_EVERY_S = 0.25       # <= 4 bus events per second per pack
GPU_CACHE_S = 60.0
DL_MAX_AGE_S = 7 * 86400     # packs/.dl leftovers (canceled / failed downloads) older than this go at startup
MODEL_REQUIRED = ("model.bin", "config.json")   # without these faster-whisper cannot load a snapshot at all


def _which(name: str) -> str | None:
    return shutil.which(name)       # one seam: tests fake PATH lookups here, not in the shared shutil module


class PackError(Exception):
    pass


class _Cancelled(Exception):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _ascii(s) -> str:
    return str(s).encode("ascii", "replace").decode("ascii")[:400]


def _ua() -> str:
    return f"WardogClips/{paths.APP_VERSION}"


def _packs_dir() -> Path:
    return Path(paths.PACKS_DIR)     # read at call time: tests point paths.PACKS_DIR at a temp dir


def pack_dir(pack_id: str) -> Path:
    return _packs_dir() / pack_id


def _manifest(pack_id: str) -> dict | None:
    f = pack_dir(pack_id) / "PACK.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def installed(pack_id: str) -> bool:
    return _manifest(pack_id) is not None


# -- what the rest of the app asks ---------------------------------------------------------------------------------
def cuda_bin_dirs() -> list[Path]:
    """packs/cuda/nvidia/<lib>/bin dirs of an installed cuda pack (stt/whisper wires the same ones)."""
    if not installed("cuda"):
        return []
    base = pack_dir("cuda") / "nvidia"
    return [base / s / "bin" for s in CUDA_SUBDIRS if (base / s / "bin").is_dir()]


def deno_exe() -> Path | None:
    """packs/deno/deno.exe when the deno pack is installed (media/vods: `--js-runtimes deno:<path>`)."""
    exe = pack_dir("deno") / "deno.exe"
    return exe if installed("deno") and exe.is_file() else None


def _vtuple(v) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "")))


def bundled_ytdlp_version() -> str:
    try:
        from yt_dlp.version import __version__ as v
        return str(v)
    except Exception:            # noqa: BLE001 -- no bundled yt-dlp: any pack version is newer
        return ""


def ytdlp_path() -> Path | None:
    """packs/ytdlp (for the yt-dlp child's PYTHONPATH) when installed AND newer than the bundled yt-dlp -- after an
    app update bundles a newer one, an old pack must not win."""
    m = _manifest("ytdlp")
    d = pack_dir("ytdlp")
    if not m or not (d / "yt_dlp").is_dir():
        return None
    return d if _vtuple(m.get("version")) > _vtuple(bundled_ytdlp_version()) else None


_dll_handles: list = []      # os.add_dll_directory handles: kept so the dirs stay registered
_dll_wired: set = set()


# -- per-pack install locks (cross-process) -------------------------------------------------------------------------
def _lock_path(pack_id: str) -> Path:
    return _packs_dir() / ".locks" / (re.sub(r"[^A-Za-z0-9._-]", "-", pack_id) + ".lock")


class _PackLock:
    """An exclusive OS lock on byte 0 of packs/.locks/<id>.lock, held for a whole install (or a remove / sweep of that
    id). Non-blocking: acquire() is False when any other handle -- this process or another -- holds it. The OS drops
    it when the process dies, so a crash never leaves a stale lock."""

    def __init__(self, pack_id: str):
        self.path = _lock_path(pack_id)
        self._f = None

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            f = open(self.path, "a+b")
        except OSError:
            return False
        try:
            f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:                                   # pragma: no cover -- the app is Windows-only
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._f = f
        return True

    def release(self) -> None:
        f, self._f = self._f, None
        if f is None:
            return
        try:
            f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            f.close()


def _locked_elsewhere(pack_id: str) -> bool:
    """True while some handle (an install in this process or another one) holds the id's lock."""
    lk = _PackLock(pack_id)
    if lk.acquire():
        lk.release()
        return False
    return True


# which pack a packs/.dl file belongs to (by the wheel / zip names the installers use)
_DL_OWNERS = (("nvidia", "cuda"), ("deno-", "deno"), ("yt_dlp", "ytdlp"), ("yt-dlp", "ytdlp"))


def _dl_owner(name: str) -> str | None:
    low = name.lower()
    return next((pid for pre, pid in _DL_OWNERS if low.startswith(pre)), None)


def _clear_dl(pack_id: str) -> int:
    """Delete packs/.dl files (finished downloads and .part resumes) of one pack. Returns the count."""
    n = 0
    dl = _packs_dir() / ".dl"
    try:
        for f in dl.iterdir() if dl.is_dir() else ():
            if f.is_file() and _dl_owner(f.name) == pack_id:
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
    except OSError:
        pass
    return n


def wire_dlls() -> int:
    """Register the cuda pack's DLL dirs (os.add_dll_directory + PATH; ctranslate2 resolves cublas through a plain
    LoadLibrary that searches PATH). Idempotent; never deletes anything. Returns the number of dirs."""
    dirs = cuda_bin_dirs()
    for d in dirs:
        key = str(d).lower()
        if key not in _dll_wired:
            try:
                _dll_handles.append(os.add_dll_directory(str(d)))
                _dll_wired.add(key)
            except (OSError, AttributeError):
                pass
        if str(d) not in os.environ.get("PATH", ""):
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
    return len(dirs)


def sweep_leftovers(max_dl_age_s: float = DL_MAX_AGE_S) -> dict:
    """Drop what interrupted installs / removes left: <id>.tmp, <id>.old, a pack dir whose PACK.json is gone (a remove
    that could not delete DLLs the last run still had loaded), and packs/.dl files older than `max_dl_age_s`.
    An id that is installing -- in this process (busy) or in another one (its lock is held) -- is never touched, so
    this is safe in every process on the same data dir."""
    root = _packs_dir()
    out = {"dirs": 0, "dl": 0, "skipped": 0}
    try:
        entries = list(root.iterdir()) if root.is_dir() else []
    except OSError:
        entries = []
    for p in entries:
        if not p.is_dir() or p.name.startswith("."):
            continue
        pid = p.name[:-4] if p.name.endswith((".tmp", ".old")) else p.name
        if pid not in PACK_IDS:
            continue
        if p.name == pid and (p / "PACK.json").exists():
            continue
        lk = _PackLock(pid)
        if busy(pid) or not lk.acquire():
            out["skipped"] += 1
            continue
        try:
            if p.name == pid and (p / "PACK.json").exists():    # committed while we waited for the lock
                continue
            shutil.rmtree(p, ignore_errors=True)
            out["dirs"] += 1
        finally:
            lk.release()
    dl = root / ".dl"
    now = time.time()
    try:
        files = list(dl.iterdir()) if dl.is_dir() else []
    except OSError:
        files = []
    for f in files:
        try:
            if not f.is_file() or now - f.stat().st_mtime < max_dl_age_s:
                continue
        except OSError:
            continue
        owner = _dl_owner(f.name)
        if owner and (busy(owner) or _locked_elsewhere(owner)):
            out["skipped"] += 1
            continue
        try:
            f.unlink()
            out["dl"] += 1
        except OSError:
            pass
    if out["dirs"] or out["dl"] or out["skipped"]:
        logger.info("[Packs] sweep dirs=%d dl=%d skipped=%d", out["dirs"], out["dl"], out["skipped"])
    return out


def activate() -> str:
    """main.py calls this before anything imports ctranslate2: wire the cuda pack's DLL dirs and sweep leftovers.
    The sweep skips every id an install (any process) holds, so a second launch on the same data dir is harmless.
    An install itself only ever calls wire_dlls()."""
    sw = sweep_leftovers()
    n = wire_dlls()
    have = [p for p in PACK_IDS if installed(p)]
    line = f"packs={','.join(have) or 'none'} cuda_dirs={n}"
    if sw["skipped"]:
        line += f" busy_skipped={sw['skipped']}"
    logger.info("[Packs] activate %s", line)
    return line


# -- GPU ------------------------------------------------------------------------------------------------------------
_gpu_cache: dict = {"t": 0.0, "info": None}
_gpu_lock = threading.Lock()


def _nvidia_smi() -> str | None:
    exe = _which("nvidia-smi")
    if exe:
        return exe
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    cand = Path(sysroot) / "System32" / "nvidia-smi.exe"
    return str(cand) if cand.exists() else None


def _fake_gpu_info() -> dict | None:
    """gpu_info() from a forced card list (utils/gpus.py FAKE_ENV: try the AMD / Intel / no-card paths); None = real."""
    try:
        from utils import gpus
        if gpus.source() != "fake":
            return None
        nv = next((a for a in gpus.hardware() if a["vendor"] == "nvidia"), None)
        return {"nvidia": nv is not None, "name": nv["name"] if nv else "", "vram_mb": nv["vram_mb"] if nv else 0,
                "driver": ""}
    except Exception:            # noqa: BLE001
        return None


def gpu_info(max_age: float = GPU_CACHE_S) -> dict:
    """{"nvidia", "name", "vram_mb", "driver"} from nvidia-smi's GPU query (GPU-level, never a process list)."""
    with _gpu_lock:
        if _gpu_cache["info"] is not None and time.monotonic() - _gpu_cache["t"] < max_age:
            return dict(_gpu_cache["info"])
        info = {"nvidia": False, "name": "", "vram_mb": 0, "driver": ""}
        fake = _fake_gpu_info()                # a forced card list (utils/gpus.py FAKE_ENV) wins over nvidia-smi
        exe = _nvidia_smi() if fake is None else None
        if fake is not None:
            info = fake
        if exe:
            try:
                p = subprocess.run([exe, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
                                   capture_output=True, text=True, timeout=10,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                line = next((ln for ln in (p.stdout or "").splitlines() if ln.strip()), "")
                parts = [x.strip() for x in line.split(",")]
                if p.returncode == 0 and len(parts) >= 3 and parts[0]:
                    try:
                        vram = int(float(parts[1]))
                    except ValueError:
                        vram = 0
                    info = {"nvidia": True, "name": parts[0], "vram_mb": vram, "driver": parts[2]}
            except (OSError, subprocess.SubprocessError):
                logger.info("[Packs] nvidia-smi did not answer", exc_info=True)
        _gpu_cache.update(t=time.monotonic(), info=info)
        return dict(info)


def _cuda_ready_elsewhere() -> bool:
    """A source run's venv already carries the nvidia-* wheels (stt/whisper.cuda_ready)."""
    try:
        from stt import whisper
        fn = getattr(whisper, "cuda_ready", None)
        return bool(fn()) if callable(fn) else False
    except Exception:            # noqa: BLE001
        return False


def recommended_model(gpu: dict | None = None) -> str:
    """medium.en on an NVIDIA card with room for it (with the cuda pack), small.en otherwise (CPU int8 / < 6 GB)."""
    gpu = gpu if gpu is not None else gpu_info()
    if gpu.get("nvidia") and int(gpu.get("vram_mb") or 0) >= 6000:
        return "medium.en"
    return "small.en"


# -- Hugging Face cache (models) ------------------------------------------------------------------------------------
def hf_hub_dir() -> Path:
    """The same hub dir stt/whisper.is_cached() reads."""
    return Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"


def model_repo(name: str) -> str | None:
    try:
        from faster_whisper.utils import _MODELS
        repo = _MODELS.get(name)
        if repo:
            return repo
    except Exception:            # noqa: BLE001
        pass
    if name in MODEL_REPOS_FALLBACK:
        return MODEL_REPOS_FALLBACK[name]
    return f"Systran/faster-whisper-{name}" if name else None


def _repo_dir(repo: str) -> Path:
    return hf_hub_dir() / ("models--" + repo.replace("/", "--"))


def _models_file() -> Path:
    return _packs_dir() / "models.json"


def _read_models() -> dict:
    """packs/models.json = {"repos": {repo: {"commit", "files": {name: size}, "dir", "installed"}}}: the models THIS
    app downloaded (the completeness reference, and the list an uninstaller may delete -- never a model the user
    already had in the shared Hugging Face cache)."""
    try:
        d = json.loads(_models_file().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and isinstance(d.get("repos"), dict) else {"repos": {}}
    except (OSError, ValueError):
        return {"repos": {}}


_models_lock = threading.Lock()


def _write_models(update) -> None:
    with _models_lock:
        d = _read_models()
        update(d["repos"])
        f = _models_file()
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
            os.replace(tmp, f)
        except OSError as e:
            logger.warning("[Packs] models.json write failed err=%s", _ascii(e))


def _file_size(p: Path) -> int:
    """Size of the file p resolves to (a snapshot entry is a symlink to blobs/ or, on Windows without symlinks, the
    file itself); -1 when missing / dangling."""
    try:
        return p.stat().st_size
    except OSError:
        return -1


def snapshot_complete(repo: str) -> bool:
    """True only when the repo's snapshot can really be loaded offline: the snapshot refs/main points at (else any)
    has model.bin + config.json resolving to non-empty files; when this app downloaded it (packs/models.json), every
    listed file at its listed size; otherwise (a cache some other tool filled) no blobs/*.incomplete left over.
    snapshot_download places config.json / tokenizer.json long before the GBs of model.bin, so "a snapshot dir
    exists" (the old check) called an interrupted download installed. No network."""
    if not repo:
        return False
    root = _repo_dir(repo)
    snaps = root / "snapshots"
    try:
        commit = (root / "refs" / "main").read_text(encoding="utf-8").strip()
    except OSError:
        commit = ""
    cands: list[Path] = []
    if commit and (snaps / commit).is_dir():
        cands = [snaps / commit]
    else:
        try:
            cands = sorted((d for d in snaps.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
        except OSError:
            return False
    ours = _read_models()["repos"].get(repo)
    for snap in cands:
        if any(_file_size(snap / f) <= 0 for f in MODEL_REQUIRED):
            continue
        if ours and ours.get("commit") == snap.name:
            files = ours.get("files") or {}
            if all(_file_size(snap / f) == int(sz) for f, sz in files.items()):
                return True
            continue
        try:
            if any((root / "blobs").glob("*.incomplete")):
                continue
        except OSError:
            pass
        return True
    return False


def model_cached(name: str) -> bool:
    """The model `name` is completely in the HF cache (snapshot_complete of the repo faster-whisper loads)."""
    return snapshot_complete(model_repo(name) or "")


def _model_names() -> tuple:
    try:
        from core.settings import STT_MODELS
        return tuple(STT_MODELS)
    except Exception:            # noqa: BLE001
        return tuple(MODEL_SIZES_MB)


# -- jobs -----------------------------------------------------------------------------------------------------------
class _Job:
    def __init__(self, pack_id: str, bus):
        self.id = pack_id
        self.bus = bus
        self.state = "downloading"
        self.progress = 0.0
        self.bytes = 0
        self.total = 0
        self.error = ""
        self.reason = ""
        self.version = ""
        self.cancel = threading.Event()
        self.thread: threading.Thread | None = None
        self.started = time.time()
        self._last_pub = 0.0
        self._last_state = ""

    @property
    def running(self) -> bool:
        return self.state in ("downloading", "extracting")

    def set(self, force: bool = False, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)
        if self.total > 0 and self.state == "downloading":
            self.progress = max(0.0, min(1.0, self.bytes / self.total))
        self.publish(force=force)

    def publish(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self.state == self._last_state and now - self._last_pub < PUBLISH_EVERY_S:
            return
        self._last_pub, self._last_state = now, self.state
        bus = self.bus
        if bus is None:
            return
        try:
            bus.publish("packs", **_status_one(self.id))
        except Exception:        # noqa: BLE001 -- a bus problem never stops a download
            logger.debug("[Packs] publish failed", exc_info=True)


_jobs: dict[str, _Job] = {}
_jobs_lock = threading.RLock()     # start() reads the status while holding it
_removing: set[str] = set()        # ids a remove() is deleting right now (start() refuses them meanwhile)
_notes: dict[str, str] = {}        # a refused remove's plain reason, shown as the pack's note until the next action


def _valid_id(pack_id: str) -> bool:
    if pack_id in PACK_IDS:
        return True
    return pack_id.startswith(MODEL_PREFIX) and pack_id[len(MODEL_PREFIX):] in _model_names()


def busy(pack_id: str) -> bool:
    with _jobs_lock:
        j = _jobs.get(pack_id)
        return bool(j and j.thread and j.thread.is_alive())


def _default_bus():
    try:
        from core.events import bus
        return bus
    except Exception:            # noqa: BLE001
        return None


def _publish_status(pack_id: str, bus=None) -> None:
    bus = bus if bus is not None else _default_bus()
    if bus is None:
        return
    try:
        bus.publish("packs", **_status_one(pack_id))
    except Exception:            # noqa: BLE001
        pass


def start(pack_id: str, bus=None, force: bool = False) -> dict:
    """Start a background install of `pack_id` (a PACK_IDS entry or "model:<name>"); idempotent while it runs.
    `force` (ytdlp): install the latest even when it is not newer than the bundled yt-dlp. Raises ValueError for an
    unknown id; everything else ends up in the returned / published state. While a remove of the same id runs,
    nothing starts (the returned status says so)."""
    if not _valid_id(pack_id):
        raise MsgError(msg("server.packs.unknown", "Unknown add-on: {id}", id=pack_id))
    bus = bus if bus is not None else _default_bus()
    with _jobs_lock:
        cur = _jobs.get(pack_id)
        if cur and cur.thread and cur.thread.is_alive():
            return _status_one(pack_id)
        if pack_id in _removing:
            out = _status_one(pack_id)
            out["note"] = msg("server.pack.being_removed", "It is being removed right now. Try again in a moment.")["message"]
            annotate(out, "note")
            return out
        _notes.pop(pack_id, None)
        job = _Job(pack_id, bus)
        _jobs[pack_id] = job
        job.thread = threading.Thread(target=_worker, args=(job, force), name=f"pack-{pack_id}", daemon=True)
        job.thread.start()
    logger.info("[Packs] install started pack=%s", pack_id)
    job.publish(force=True)
    return _status_one(pack_id)


def cancel(pack_id: str) -> bool:
    with _jobs_lock:
        j = _jobs.get(pack_id)
    if not j or not j.thread or not j.thread.is_alive():
        return False
    j.cancel.set()
    j.reason = CANCELING
    j.publish(force=True)
    logger.info("[Packs] cancel requested pack=%s", pack_id)
    return True


def wait(pack_id: str, timeout: float = 60.0) -> dict:
    """Block until the install thread ends (tests, the live check); returns the status."""
    with _jobs_lock:
        j = _jobs.get(pack_id)
    if j and j.thread:
        j.thread.join(timeout)
    return _status_one(pack_id)


MSG_INSTALLING = msg("server.packs.installing", "It is installing right now. Cancel the install first.")["message"]
MSG_INSTALLING_ELSEWHERE = msg("server.pack.installing_elsewhere",
                               "Another WardogClips window is installing it right now. Try again when that is "
                               "done.")["message"]
MSG_TRANSCRIBING = msg("server.pack.transcribing",
                       "A transcription is using the speech model right now. Remove it when the transcription is "
                       "done, or stop that job in Jobs.")["message"]
MSG_CONFIGURED = msg("server.pack.configured",
                     "WardogClips is set to use this speech model. Choose another one in Settings > Speech-to-text "
                     "first, then remove this one.")["message"]
MSG_LOCKED_FILES = msg("server.pack.locked_files",
                       "Some of its files are still in use, so they could not all be deleted. Restart WardogClips and "
                       "remove it again.")["message"]
CANCELING = msg("server.pack.canceling", "Canceling...")["message"]


def reason_error(res: dict) -> MsgError:
    """A refused remove_ex() result as a translatable error (its reason is one of the texts above)."""
    meta = text_i18n(res.get("reason") or "") or {"code": "", "params": {}}
    return MsgError({"message": res.get("reason") or "", **meta})


def _configured_model(cfg) -> str:
    if cfg is None:
        try:
            from core import settings as sm
            cfg = sm.load()
        except Exception:        # noqa: BLE001 -- no settings: nothing configured to protect
            return ""
    try:
        from core import speechlang
        # the model really loaded: an English-only choice runs as its multilingual twin when other languages are on
        return speechlang.effective_model(str(cfg.stt.model or ""), getattr(cfg.stt, "language", "en"))
    except AttributeError:
        return ""


def _result(ok: bool, code: str, reason: str = "") -> dict:
    return {"ok": ok, "code": code, "reason": reason}


def remove_ex(pack_id: str, cfg=None) -> dict:
    """Delete packs/<id> (a model: its models--<org>--<repo> folder in the HF cache) without ever blocking on other
    work. Returns {"ok", "code", "reason"}: code ok | missing | unknown | installing | in_use | locked. Refused, with
    a plain reason (also shown as the pack's note), while the id installs here or in another WardogClips on this data
    dir, while a transcription holds the speech engine, and for the model the settings use (queued transcribe jobs
    load that one). The slow delete runs outside _jobs_lock (status() / busy() never stall); start() of the same id
    is refused meanwhile, and only the finished job seen at the start is dropped -- never a live one."""
    if not _valid_id(pack_id):
        return _result(False, "unknown", msg("server.pack.unknown_one", "Unknown add-on.")["message"])
    with _jobs_lock:
        if busy(pack_id) or pack_id in _removing:
            return _refuse(pack_id, _result(False, "installing", MSG_INSTALLING))
        _removing.add(pack_id)
        captured = _jobs.get(pack_id)
    res = _result(False, "missing")
    try:
        lk = _PackLock(pack_id)
        if not lk.acquire():
            res = _result(False, "installing", MSG_INSTALLING_ELSEWHERE)
        else:
            try:
                res = _remove_model(pack_id, cfg) if pack_id.startswith(MODEL_PREFIX) else _remove_pack(pack_id)
            finally:
                lk.release()
    finally:
        with _jobs_lock:
            _removing.discard(pack_id)
            j = _jobs.get(pack_id)
            if res["ok"] and j is not None and j is captured and not (j.thread and j.thread.is_alive()):
                _jobs.pop(pack_id, None)
    if res["reason"]:
        return _refuse(pack_id, res)
    _notes.pop(pack_id, None)
    _publish_status(pack_id)
    return res


def _refuse(pack_id: str, res: dict) -> dict:
    logger.info("[Packs] remove refused pack=%s code=%s", pack_id, res["code"])
    _notes[pack_id] = res["reason"]
    _publish_status(pack_id)
    return res


def _remove_pack(pack_id: str) -> dict:
    d = pack_dir(pack_id)
    n_dl = _clear_dl(pack_id)                 # canceled / failed downloads of this pack go with it
    if not d.exists():
        return _result(False, "missing")
    # PACK.json first: from here on the pack is not installed even when a DLL still loaded by this process keeps its
    # folder alive (sweep_leftovers removes the leftover at the next start)
    try:
        (d / "PACK.json").unlink(missing_ok=True)
    except OSError as e:
        logger.warning("[Packs] remove failed pack=%s err=%s", pack_id, _ascii(e))
        return _result(False, "locked", MSG_LOCKED_FILES)
    shutil.rmtree(d, ignore_errors=True)
    logger.info("[Packs] removed pack=%s leftover=%s dl_files=%d", pack_id, d.exists(), n_dl)
    return _result(True, "ok")


def _remove_model(pack_id: str, cfg) -> dict:
    name = pack_id[len(MODEL_PREFIX):]
    repo = model_repo(name)
    d = _repo_dir(repo) if repo else None
    if not d or not d.is_dir():
        return _result(False, "missing")
    if name == _configured_model(cfg):
        return _result(False, "in_use", MSG_CONFIGURED)
    eng = eng_lock = None
    try:
        from stt import whisper
        eng = whisper.engine
        eng_lock = getattr(eng, "_lock", None)
    except Exception:            # noqa: BLE001 -- no speech engine importable: nothing can hold the model
        pass
    # a transcription holds the engine lock for its whole decode: refuse at once instead of waiting tens of minutes,
    # and hold it through unload + delete so no load / transcribe starts in the middle
    if eng_lock is not None and not eng_lock.acquire(blocking=False):
        return _result(False, "in_use", MSG_TRANSCRIBING)
    try:
        try:
            if eng is not None and eng.status().get("loaded") == name:
                eng.unload()     # the resident model maps model.bin: Windows refuses to delete it while loaded
        except Exception:        # noqa: BLE001
            logger.debug("[Packs] engine unload before remove failed", exc_info=True)
        shutil.rmtree(d, ignore_errors=True)
    finally:
        if eng_lock is not None:
            eng_lock.release()
    _write_models(lambda repos: repos.pop(repo, None))
    if d.exists():
        logger.warning("[Packs] model remove left files model=%s repo=%s", name, repo)
        return _result(False, "locked", MSG_LOCKED_FILES)
    logger.info("[Packs] removed model=%s repo=%s", name, repo)
    return _result(True, "ok")


def remove(pack_id: str, cfg=None) -> bool:
    """remove_ex() as a bool (the old API): False when refused or there was nothing to remove."""
    return bool(remove_ex(pack_id, cfg)["ok"])


def _worker(job: _Job, force: bool) -> None:
    pid = job.id
    t0 = time.monotonic()
    lk = _PackLock(pid)
    if not lk.acquire():
        text = msg("server.pack.locked_elsewhere",
                   "Another WardogClips window is installing this add-on right now. Try again when that is "
                   "done.")["message"]
        job.set(force=True, state="failed", error=text, reason="")
        logger.warning("[Packs] install refused pack=%s err=locked by another process", pid)
        return
    try:
        if pid == "cuda":
            _install_cuda(job)
        elif pid == "deno":
            _install_deno(job)
        elif pid == "ytdlp":
            _install_ytdlp(job, force)
        else:
            _install_model(job, pid[len(MODEL_PREFIX):])
        job.set(force=True, state="done", progress=1.0, error="",
                reason=job.reason if job.reason != CANCELING else "")
        logger.info("[Packs] install done pack=%s version=%s secs=%.1f", pid, job.version or "-", time.monotonic() - t0)
    except _Cancelled:
        job.set(force=True, state="cancelled", reason="")
        logger.info("[Packs] install canceled pack=%s bytes=%d", pid, job.bytes)
    except Exception as e:       # noqa: BLE001 -- never out of the thread
        err = _ascii(e) or e.__class__.__name__
        job.set(force=True, state="failed", error=err, reason="")
        logger.warning("[Packs] install failed pack=%s err=%s", pid, err, exc_info=not isinstance(e, PackError))
    finally:
        try:
            tmp = _packs_dir() / f"{pid}.tmp"
            if pid in PACK_IDS and tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
        finally:
            lk.release()


# -- network --------------------------------------------------------------------------------------------------------
def _session():
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = _ua()
    return s


def _host_ok(host: str) -> bool:
    return host in ALLOWED_HOSTS or any(host.endswith(s) for s in ALLOWED_HOST_SUFFIXES)


def _check_host(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if urlparse(url).scheme != "https" or not _host_ok(host):
        raise PackError(msg("server.pack.err.host", "WardogClips refused download host {host}: it is not one of the "
                            "fixed add-on hosts.", host=repr(host or url))["message"])


def _get(sess, url: str, headers: dict | None = None, stream: bool = False):
    """GET with every redirect hop checked against ALLOWED_HOSTS (requests would follow them unchecked)."""
    for _ in range(6):
        _check_host(url)
        r = sess.get(url, headers=headers or {}, stream=stream, allow_redirects=False,
                     timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            url = urljoin(url, r.headers["Location"])
            r.close()
            continue
        return r
    raise PackError(msg("server.pack.err.redirects", "The download was redirected too many times.")["message"])


def _get_json(sess, url: str) -> dict:
    r = _get(sess, url)
    if r.status_code != 200:
        raise PackError(msg("server.pack.err.http", "{host} answered HTTP {status} for {file}.",
                            host=urlparse(url).hostname, status=r.status_code, file=urlparse(url).path)["message"])
    return r.json()


def _pypi_file(sess, name: str, ver: str | None, want: str) -> dict:
    """{url, filename, size, sha256, version, requires} of `name`'s wheel whose file name contains `want`."""
    url = PYPI_JSON_VER.format(name=name, ver=ver) if ver else PYPI_JSON.format(name=name)
    j = _get_json(sess, url)
    for u in j.get("urls") or []:
        fn = u.get("filename", "")
        if fn.endswith(".whl") and want in fn:
            sha = (u.get("digests") or {}).get("sha256", "")
            if not sha:
                raise PackError(msg("server.pack.err.no_sha", "PyPI gave no checksum (sha256) for {file}.",
                                    file=fn)["message"])
            return {"url": u["url"], "filename": fn, "size": int(u.get("size") or 0), "sha256": sha,
                    "version": (j.get("info") or {}).get("version", ver or ""),
                    "requires": (j.get("info") or {}).get("requires_dist") or []}
    raise PackError(msg("server.pack.err.no_wheel", "PyPI has no {kind} package of {name} {version}.", kind=want,
                        name=name, version=ver or "latest")["message"])


def _hash_file(path: Path, h, job: _Job | None = None):
    """Feed the file into hasher h (cancel-checked between 4 MB reads); returns h."""
    with open(path, "rb") as f:
        while True:
            if job is not None and job.cancel.is_set():
                raise _Cancelled()
            b = f.read(CHUNK * 4)
            if not b:
                return h
            h.update(b)


def _file_sha256(path: Path, job: _Job | None = None) -> "hashlib._Hash":
    return _hash_file(path, hashlib.sha256(), job)


def _git_blob_hasher(size: int):
    """sha1 over git's blob header + content = the blobId Hugging Face lists for a non-LFS file."""
    h = hashlib.sha1()
    h.update(b"blob %d\0" % size)
    return h


def _stream(job: _Job, sess, url: str, part: Path, size: int, new_hash, base: int, label: str) -> str:
    """Stream `url` into `part`, resuming with Range from what `part` already holds; returns the hex digest of the
    whole file (new_hash() makes a fresh hasher). job.bytes runs from `base`. Raises on a short download."""
    part.parent.mkdir(parents=True, exist_ok=True)
    have = part.stat().st_size if part.exists() else 0
    if size and have > size:
        part.unlink(missing_ok=True)
        have = 0
    h = _hash_file(part, new_hash(), job) if have else new_hash()
    if not (size and have == size):
        headers = {"Range": f"bytes={have}-"} if have else {}
        r = _get(sess, url, headers=headers, stream=True)
        try:
            if r.status_code == 206 and have:
                mode = "ab"
                logger.info("[Packs] resume file=%s from=%d", label, have)
            elif r.status_code == 200:
                mode, have, h = "wb", 0, new_hash()      # no range support: start over
            elif r.status_code == 416 and have:
                mode = None                                # nothing left to fetch
            else:
                raise PackError(msg("server.pack.err.http", "{host} answered HTTP {status} for {file}.",
                                    host=urlparse(r.url or url).hostname, status=r.status_code, file=label)["message"])
            if mode:
                job.set(bytes=base + have)
                with open(part, mode) as f:
                    for chunk in r.iter_content(CHUNK):
                        if job.cancel.is_set():
                            raise _Cancelled()
                        if not chunk:
                            continue
                        f.write(chunk)
                        h.update(chunk)
                        have += len(chunk)
                        job.set(bytes=base + have)
        finally:
            r.close()
    if size and have != size:
        raise PackError(msg("server.pack.err.short", "The download of {file} stopped at {have} of {size} bytes. Try "
                            "again to resume it.", file=label, have=have, size=size)["message"])
    return h.hexdigest()


def _fetch(job: _Job, sess, url: str, filename: str, size: int, sha256: str, base: int) -> Path:
    """Stream `url` to packs/.dl/<filename> (via .part, resumed with Range), sha256-checked. job.bytes runs from
    `base` (the bytes of the files before this one) so a multi-file pack shows one progress bar."""
    dl = _packs_dir() / ".dl"
    dl.mkdir(parents=True, exist_ok=True)
    final = dl / filename
    part = dl / (filename + ".part")
    sha256 = sha256.lower()
    if final.exists():
        if (not size or final.stat().st_size == size) and _file_sha256(final, job).hexdigest() == sha256:
            job.set(bytes=base + final.stat().st_size)
            return final
        final.unlink(missing_ok=True)
    digest = _stream(job, sess, url, part, size, hashlib.sha256, base, filename)
    if digest != sha256:
        part.unlink(missing_ok=True)      # a corrupt part must not be resumed
        raise PackError(msg("server.pack.err.sha256", "{file} failed its check (sha256 mismatch), so the download was "
                            "damaged. Try again.", file=filename)["message"])
    os.replace(part, final)
    return final


def _safe_target(root: Path, member: str) -> Path:
    t = (root / member).resolve()
    if not str(t).lower().startswith(str(root.resolve()).lower() + os.sep.lower()) and t != root.resolve():
        raise PackError(msg("server.pack.err.member", "The archive held a file outside the add-on folder ({file}), so "
                            "it was refused.", file=repr(member))["message"])
    return t


def _extract(job: _Job, zpath: Path, dest: Path, keep, rename=None) -> tuple[int, int]:
    """Extract the members `keep(name)` accepts into dest (zip-slip checked). Returns (files, bytes)."""
    n = b = 0
    with zipfile.ZipFile(zpath) as z:
        infos = [i for i in z.infolist() if not i.is_dir() and keep(i.filename)]
        for k, info in enumerate(infos):
            if job.cancel.is_set():
                raise _Cancelled()
            name = rename(info.filename) if rename else info.filename
            target = _safe_target(dest, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, CHUNK)
            n += 1
            b += info.file_size
            job.set(progress=(k + 1) / max(1, len(infos)))
    return n, b


def _commit(job: _Job, pack_id: str, tmp: Path, version: str, files: int, nbytes: int) -> None:
    """<id>.tmp -> <id> (an existing install moves aside first), then PACK.json LAST = installed."""
    final = pack_dir(pack_id)
    if final.exists():
        old = _packs_dir() / f"{pack_id}.old"
        shutil.rmtree(old, ignore_errors=True)
        try:
            os.replace(final, old)
        except OSError as e:
            raise PackError(msg("server.pack.err.in_use", "The installed add-on {id} is in use ({error}). Restart "
                                "WardogClips and try again.", id=pack_id, error=_ascii(e))["message"])
        shutil.rmtree(old, ignore_errors=True)
    os.replace(tmp, final)
    man = {"id": pack_id, "version": version, "installed": _now_iso(), "files": files, "bytes": nbytes}
    (final / "PACK.json").write_text(json.dumps(man, indent=2), encoding="utf-8")
    job.version = version


def _fresh_tmp(pack_id: str) -> Path:
    tmp = _packs_dir() / f"{pack_id}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    return tmp


# -- the packs ------------------------------------------------------------------------------------------------------
def _install_cuda(job: _Job) -> None:
    if not gpu_info().get("nvidia"):
        raise PackError(msg("server.pack.err.no_nvidia", "No NVIDIA graphics card was found, so the speech engine for "
                            "the graphics card would not be used.")["message"])
    sess = _session()
    wheels = [_pypi_file(sess, n, v, "win_amd64") for n, v in CUDA_WHEELS]
    job.set(total=sum(w["size"] for w in wheels), bytes=0)
    files, base = [], 0
    for w in wheels:
        files.append(_fetch(job, sess, w["url"], w["filename"], w["size"], w["sha256"], base))
        base += w["size"]
    job.set(force=True, state="extracting", progress=0.0)
    tmp = _fresh_tmp("cuda")
    n = b = 0

    # only the DLLs (+ the license files) -- the wheels' include/ and lib/ are for compiling against CUDA
    def keep(name: str) -> bool:
        low = name.lower()
        return low.startswith("nvidia/") and ("/bin/" in low or low.endswith("license.txt"))
    for f in files:
        dn, db = _extract(job, f, tmp, keep)
        n, b = n + dn, b + db
    if not any(tmp.glob("nvidia/cublas/bin/cublas64_*.dll")) or not any(tmp.glob("nvidia/cudnn/bin/cudnn64_*.dll")):
        raise PackError(msg("server.pack.err.cuda_dlls", "The CUDA packages did not contain the cublas64 / cudnn64 "
                            "DLLs.")["message"])
    _commit(job, "cuda", tmp, " / ".join(f"{nm.split('-')[1]} {v}" for nm, v in CUDA_WHEELS), n, b)
    for f in files:
        f.unlink(missing_ok=True)       # 1.3 GB of wheels are not needed once extracted
    wire_dlls()                         # NOT activate(): its sweep is for startup only
    later = _reload_engine()
    job.reason = (msg("server.pack.ready_later", "Ready: used from the next transcription (after the one running "
                      "now).")["message"] if later
                  else msg("server.pack.ready", "Ready: used from the next transcription.")["message"])


def _reload_engine() -> bool:
    """Drop the resident (CPU) model so the next transcription loads on CUDA. A running transcription holds the
    engine lock for its whole decode, so then the reload runs on a helper thread once it ends instead of keeping
    this install at its last step. Returns True when it was deferred."""
    def do():
        try:
            from stt import whisper
            fn = getattr(whisper, "reload_engine", None)
            if callable(fn):
                fn()
            else:
                whisper.engine.unload()
        except Exception:               # noqa: BLE001
            logger.warning("[Packs] could not tell the STT engine to reload", exc_info=True)
    lock = None
    try:
        from stt import whisper
        lock = getattr(whisper.engine, "_lock", None)
    except Exception:                   # noqa: BLE001
        pass
    if lock is None or lock.acquire(blocking=False):
        try:
            do()
        finally:
            if lock is not None:
                lock.release()
        return False
    def later():
        with lock:                      # waits for the running transcription, off the install thread
            do()
    threading.Thread(target=later, name="pack-cuda-reload", daemon=True).start()
    logger.info("[Packs] engine busy: reload deferred until the running transcription ends")
    return True


def _install_deno(job: _Job) -> None:
    sess = _session()
    job.set(total=DENO_SIZE, bytes=0)
    z = _fetch(job, sess, DENO_URL, f"deno-{DENO_VERSION}-x86_64-pc-windows-msvc.zip", DENO_SIZE, DENO_SHA256, 0)
    job.set(force=True, state="extracting", progress=0.0)
    tmp = _fresh_tmp("deno")
    n, b = _extract(job, z, tmp, lambda name: name.lower().rsplit("/", 1)[-1] == "deno.exe",
                    rename=lambda name: "deno.exe")
    if not (tmp / "deno.exe").is_file():
        raise PackError(msg("server.pack.err.no_deno", "The Deno download held no deno.exe.")["message"])
    _commit(job, "deno", tmp, DENO_VERSION, n, b)
    z.unlink(missing_ok=True)


def _install_ytdlp(job: _Job, force: bool) -> None:
    sess = _session()
    main = _pypi_file(sess, "yt-dlp", None, "py3-none-any")
    bundled = bundled_ytdlp_version()
    have = (_manifest("ytdlp") or {}).get("version", "")
    if not force and _vtuple(main["version"]) <= max(_vtuple(bundled), _vtuple(have)):
        job.version = have or bundled
        job.reason = msg("server.pack.ytdlp_current", "Already up to date: you have the newest downloader (yt-dlp "
                         "{version}).", version=have or bundled)["message"]
        return
    # yt-dlp[default] pins the yt-dlp-ejs it was released with: take that version, else the latest
    ejs_ver = None
    for req in main["requires"]:
        m = re.match(r"\s*yt-dlp-ejs\s*==\s*([\w.]+)", req)
        if m:
            ejs_ver = m.group(1)
            break
    wheels = [main]
    try:
        wheels.append(_pypi_file(sess, "yt-dlp-ejs", ejs_ver, "py3-none-any"))
    except PackError:
        logger.info("[Packs] yt-dlp-ejs not found on PyPI -- yt-dlp alone")
    job.set(total=sum(w["size"] for w in wheels), bytes=0)
    files, base = [], 0
    for w in wheels:
        files.append(_fetch(job, sess, w["url"], w["filename"], w["size"], w["sha256"], base))
        base += w["size"]
    job.set(force=True, state="extracting", progress=0.0)
    tmp = _fresh_tmp("ytdlp")
    n = b = 0
    for f in files:
        dn, db = _extract(job, f, tmp, lambda name: True)
        n, b = n + dn, b + db
    if not (tmp / "yt_dlp" / "__init__.py").is_file():
        raise PackError(msg("server.pack.err.no_ytdlp", "The yt-dlp package held no yt_dlp folder.")["message"])
    on_disk = sum(1 for f in tmp.rglob("*") if f.is_file())
    if on_disk != n:
        raise PackError(msg("server.pack.err.extract", "The yt-dlp update was not extracted completely ({done} of "
                            "{total} files). Try again.", done=on_disk, total=n)["message"])
    _commit(job, "ytdlp", tmp, main["version"], n, b)
    for f in files:
        f.unlink(missing_ok=True)
    job.reason = msg("server.pack.ytdlp_updated", "Updated to yt-dlp {version} (built-in {bundled}).",
                     version=main["version"], bundled=bundled or "-")["message"]


def _hf_listing(sess, repo: str) -> tuple[str, list[dict]]:
    """(commit, files) of the repo's main revision from the Hub API -- only the files faster-whisper loads
    (MODEL_ALLOW), each {name, size, digest, algo}: LFS files carry a sha256, small ones git's blob sha1."""
    j = _get_json(sess, HF_API_REV.format(repo=repo))
    commit = str(j.get("sha") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise PackError(msg("server.pack.err.no_revision", "The model listing of {repo} gave no revision.",
                            repo=repo)["message"])
    files = []
    for sib in j.get("siblings") or []:
        fn = str(sib.get("rfilename") or "")
        if "/" in fn or "\\" in fn or not any(fnmatch.fnmatch(fn, pat) for pat in MODEL_ALLOW):
            continue
        lfs = sib.get("lfs") or {}
        size = int(lfs.get("size") or sib.get("size") or 0)
        if lfs.get("sha256"):
            files.append({"name": fn, "size": size, "digest": str(lfs["sha256"]).lower(), "algo": "sha256"})
        elif sib.get("blobId"):
            files.append({"name": fn, "size": size, "digest": str(sib["blobId"]).lower(), "algo": "git-sha1"})
        else:
            raise PackError(msg("server.pack.err.no_checksum", "The model listing of {repo} gave no checksum for "
                                "{file}.", repo=repo, file=fn)["message"])
    names = {f["name"] for f in files}
    missing = [f for f in MODEL_REQUIRED if f not in names]
    if missing:
        raise PackError(msg("server.pack.err.model_files", "The model {repo} has no {files}.", repo=repo,
                            files=", ".join(missing))["message"])
    return commit, files


def _install_model(job: _Job, name: str) -> None:
    """Download the model's files ourselves into the Hugging Face cache layout faster-whisper reads offline
    (models--<org>--<repo>/snapshots/<commit>/<file> + refs/main), not with huggingface_hub.snapshot_download.
    Why: snapshot_download fetches Xet-backed repos through hf_xet, a Rust session our cancel cannot stop (a raise
    inside its progress callback is swallowed; only KeyboardInterrupt aborts it), and HF_HUB_DISABLE_XET is read
    once at import, so switching it for one download means patching huggingface_hub.constants process-wide. The
    plain resolve URLs serve every file over HTTPS with Range, so this path gets what every other pack has: cancel at
    the next 1 MB chunk, resume from blobs/<etag>.incomplete (the same name huggingface_hub uses), a checksum on every
    file (LFS sha256 / git blob sha1 from the listing), every redirect hop host-checked, and completeness judged
    against the repo listing: a file only enters the snapshot once it is whole and verified, refs/main and
    packs/models.json are written last."""
    repo = model_repo(name)
    if not repo:
        raise PackError(msg("server.pack.err.no_source", "No download source is known for the model {name}.",
                            name=name)["message"])
    job.version = repo
    if model_cached(name):
        job.reason = msg("server.pack.already_downloaded", "Already downloaded.")["message"]
        return
    sess = _session()
    commit, files = _hf_listing(sess, repo)
    job.set(total=sum(f["size"] for f in files), bytes=0)
    root = _repo_dir(repo)
    snap = root / "snapshots" / commit
    blobs = root / "blobs"
    # the listing goes into packs/models.json BEFORE the first byte: from now on snapshot_complete judges this
    # snapshot against every listed file, so a failed / canceled run (model.bin there, vocabulary missing) is
    # never taken as installed
    entry = {"commit": commit, "files": {f["name"]: f["size"] for f in files}, "dir": str(root),
             "complete": False}
    _write_models(lambda repos: repos.__setitem__(repo, dict(entry)))
    base = 0
    for f in files:
        target = snap / f["name"]
        if _file_size(target) == f["size"]:          # already whole (an earlier run, or huggingface_hub)
            base += f["size"]
            job.set(bytes=base)
            continue
        part = blobs / (f["digest"] + ".incomplete")
        url = HF_RESOLVE.format(repo=repo, rev=commit, file=f["name"])
        size = f["size"]
        new_hash = hashlib.sha256 if f["algo"] == "sha256" else (lambda size=size: _git_blob_hasher(size))
        digest = _stream(job, sess, url, part, size, new_hash, base, f["name"])
        if digest != f["digest"]:
            part.unlink(missing_ok=True)
            raise PackError(msg("server.pack.err.checksum", "{file} of {repo} failed its check (checksum mismatch), "
                                "so the download was damaged. Try again.", file=f["name"], repo=repo)["message"])
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            target.unlink()                          # a dangling link / short file of an older attempt
        os.replace(part, target)
        base += size
        job.set(bytes=base)
    if job.cancel.is_set():
        raise _Cancelled()
    refs = root / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / "main").write_text(commit, encoding="utf-8")
    entry.update(complete=True, installed=_now_iso())
    _write_models(lambda repos: repos.__setitem__(repo, entry))
    if not model_cached(name):
        raise PackError(msg("server.pack.err.incomplete", "The model download finished, but the model is not "
                            "complete in the cache.")["message"])
    job.reason = msg("server.pack.downloaded", "Downloaded.")["message"]


# -- status ---------------------------------------------------------------------------------------------------------
def _job_fields(pack_id: str, installed_now: bool) -> dict:
    with _jobs_lock:
        j = _jobs.get(pack_id)
    note = _notes.get(pack_id, "")
    if j is None:
        return {"state": "done" if installed_now else "idle", "progress": 1.0 if installed_now else 0.0,
                "bytes": 0, "total": 0, "error": "", "note": note}
    return {"state": j.state, "progress": round(j.progress, 4), "bytes": int(j.bytes), "total": int(j.total),
            "error": j.error, "note": note or j.reason}


def _status_one(pack_id: str, gpu: dict | None = None) -> dict:
    if pack_id.startswith(MODEL_PREFIX):
        return _model_status(pack_id[len(MODEL_PREFIX):], gpu)
    return _pack_status(pack_id, gpu)


_TEXT_FIELDS = ("title", "desc", "reason", "note", "error")


def _pack_status(pack_id: str, gpu: dict | None = None) -> dict:
    info = PACK_INFO[pack_id]
    man = _manifest(pack_id)
    inst = man is not None
    out = {"id": pack_id, "title": info["title"], "desc": info["desc"], "installed": inst,
           "version": (man or {}).get("version", ""), "size_mb": info["size_mb"], "recommended": False,
           "available": True, "reason": ""}
    out.update(_job_fields(pack_id, inst))
    if pack_id == "cuda":
        g = gpu if gpu is not None else gpu_info()
        out["available"] = bool(g.get("nvidia"))
        elsewhere = bool(out["available"] and not inst and _cuda_ready_elsewhere())
        # cuda_ready: speech already runs on the graphics card (this pack, or the libraries some other way)
        out["cuda_ready"] = bool(out["available"] and (inst or elsewhere))
        if not out["available"]:
            out["reason"] = msg("server.pack.cuda.no_nvidia", "Not needed: this PC has no NVIDIA graphics card.")["message"]
        elif elsewhere:
            out["reason"] = msg("server.pack.cuda.elsewhere", "Not needed: this PC already has it (the CUDA libraries "
                                "are in the Python environment).")["message"]
        else:
            out["recommended"] = not inst
    elif pack_id == "deno":
        node = _which("node") is not None
        on_path = _which("deno") is not None
        # needed: YouTube downloads would not work without it (no Node.js, no deno anywhere)
        out["needed"] = bool(not inst and not node and not on_path)
        if node:
            out["reason"] = msg("server.pack.deno.node", "Not needed: Node.js on this PC already does this job.")["message"]
        elif on_path and not inst:
            out["reason"] = msg("server.pack.deno.on_path", "Not needed: Deno is already installed on this PC.")["message"]
        else:
            out["recommended"] = not inst
    elif pack_id == "ytdlp":
        out["bundled"] = bundled_ytdlp_version()
        out["active"] = ytdlp_path() is not None
        if inst and not out["active"]:
            out["reason"] = msg("server.pack.ytdlp.unused", "The built-in downloader is just as new, so this update is "
                                "not used.")["message"]
    annotate(out, *_TEXT_FIELDS)
    return out


def _model_status(name: str, gpu: dict | None = None) -> dict:
    cached = model_cached(name)
    rec = recommended_model(gpu)
    title, plain = MODEL_INFO.get(name, (msg("server.pack.model.other.title", "Speech model {name}", name=name),
                                         msg("server.pack.model.other.plain", "A speech-to-text model.")))
    desc = msg("server.pack.model_desc", "{plain} Whisper {name} (faster-whisper, {repo}).", plain=plain["message"],
               name=name, repo=model_repo(name))
    out = {"id": MODEL_PREFIX + name, "name": name, "title": title["message"], "desc": desc["message"],
           "title_i18n": {"code": title["code"], "params": title["params"]},
           "plain_i18n": {"code": plain["code"], "params": plain["params"]},
           "installed": cached, "cached": cached, "version": model_repo(name) or "",
           "size_mb": float(MODEL_SIZES_MB.get(name, 0)), "recommended": name == rec, "available": True,
           "reason": ""}
    out.update(_job_fields(MODEL_PREFIX + name, cached))
    annotate(out, "reason", "note", "error")
    return out


def status(cfg=None) -> dict:
    """{"packs": [PackStatus...], "models": [ModelStatus...], "gpu": gpu_info()} -- no network."""
    gpu = gpu_info()
    out = {"packs": [_pack_status(p, gpu) for p in PACK_IDS],
           "models": [_model_status(m, gpu) for m in _model_names()],
           "gpu": gpu, "recommended_model": recommended_model(gpu),
           "packs_dir": str(_packs_dir())}
    if cfg is not None:
        try:
            out["current_model"] = _configured_model(cfg)
            out["chosen_model"] = cfg.stt.model
        except AttributeError:
            pass
    return out
