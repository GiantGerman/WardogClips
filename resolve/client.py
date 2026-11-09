"""DaVinci Resolve scripting client.

Locating the API the way Blackmagic documents it on Windows (README in
...\\Support\\Developer\\Scripting):

    RESOLVE_SCRIPT_API = %PROGRAMDATA%\\Blackmagic Design\\DaVinci Resolve\\Support\\Developer\\Scripting
    RESOLVE_SCRIPT_LIB = C:\\Program Files\\Blackmagic Design\\DaVinci Resolve\\fusionscript.dll
    PYTHONPATH        += %RESOLVE_SCRIPT_API%\\Modules

``DaVinciResolveScript`` is imported lazily (first status / hand-off call), and only from that
Modules dir. Resolve must already be running with the target project open (and, on Studio,
Preferences > System > General > External scripting using = Local); this module never launches
it. Every failure is a ``ResolveError`` with a message fit for the UI (HTTP 503).

With Resolve installed but not running, fusionscript loads fine, but ``scriptapp("Resolve")``
takes ~4 s to give up AND HOLDS THE GIL the whole time, so a thread timeout cannot interrupt it
and the whole server would freeze. Hence the connection
probe runs in a short-lived child interpreter (``probe``; killed after ``PROBE_TIMEOUT_S``,
result cached ``PROBE_TTL_S``), and the in-process ``scriptapp`` (needed for the hand-offs) is
only called once that probe said Resolve answers, inside a daemon thread with a ~3 s timeout.

Hand-offs:
- ``send_markers``: one marker per event on the recording's MediaPoolItem (found by its
  ``File Path`` clip property, else imported into bin <cfg.resolve.bin>/<YYYY-MM-DD>),
  customData ``wclips:<event id>`` so re-sending replaces instead of duplicating.
- ``build_selects``: a new timeline with one clip per (merged) event range + a timeline marker each.
"""

import importlib
import json
import logging
import ntpath
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from resolve import export
from utils import paths
from utils.i18n import msg

logger = logging.getLogger("wclips.resolve")

DEFAULT_API = r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting"
DEFAULT_LIB = r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"
MODULE_NAME = "DaVinciResolveScript"
CONNECT_TIMEOUT_S = 3.0      # in-process scriptapp once the probe said Resolve answers
PROBE_TIMEOUT_S = 8.0        # child interpreter: ~0.3 s start + up to ~4.1 s for fusionscript to give up
PROBE_TTL_S = 3.0            # status polls within this reuse the last probe (Resolve up)
PROBE_TTL_DOWN_S = 45.0      # ... (Resolve down: each probe is a ~4 s child process; the pill's click forces one)
OP_WAIT_S = 120.0            # a second hand-off waits this long for the first to finish
MARKER_KINDS = ("kill", "assist", "death", "spotted", "voice", "manual")
CUSTOM_PREFIX = "wclips:"
SCRIPTING_HINT = ("start DaVinci Resolve, open the project, and check Preferences > System > General > "
                  "External scripting using = Local")


class ResolveError(RuntimeError):
    """Anything that stops a hand-off; the routes turn it into HTTP 503."""


class ResolveUnavailable(ResolveError):
    """The scripting module / fusionscript library is not installed or does not load."""


class ResolveNotRunning(ResolveError):
    """The module loads but Resolve does not answer."""


# ---- module location / loading ---------------------------------------------------------------
_mod_lock = threading.Lock()
_module = None
_module_error: str | None = None
_module_error_at = 0.0
_RETRY_LOAD_S = 10.0

_inflight_lock = threading.Lock()
_inflight: threading.Thread | None = None

_op_lock = threading.Lock()

_probe_lock = threading.Lock()
_probe_cache: tuple[float, dict] | None = None


def script_paths() -> dict:
    api = os.environ.get("RESOLVE_SCRIPT_API") or ""
    if not api:
        pd = os.environ.get("PROGRAMDATA")
        api = (os.path.join(pd, "Blackmagic Design", "DaVinci Resolve", "Support", "Developer", "Scripting")
               if pd else DEFAULT_API)
    lib = os.environ.get("RESOLVE_SCRIPT_LIB") or DEFAULT_LIB
    return {"api": api, "modules": os.path.join(api, "Modules"), "lib": lib}


def reset() -> None:
    """Forget the loaded module / last error (tests, or after installing Resolve)."""
    global _module, _module_error, _module_error_at, _probe_cache
    with _mod_lock:
        _module, _module_error, _module_error_at = None, None, 0.0
    _probe_cache = None


def load_module():
    """Import DaVinciResolveScript from the documented Modules dir; raises ResolveUnavailable.
    A failure is remembered for a few seconds so the status pill does not hammer the disk."""
    global _module, _module_error, _module_error_at
    with _mod_lock:
        if _module is not None:
            return _module
        if _module_error and time.monotonic() - _module_error_at < _RETRY_LOAD_S:
            raise ResolveUnavailable(_module_error)
        p = script_paths()
        mod_file = Path(p["modules"]) / f"{MODULE_NAME}.py"
        err = None
        if not mod_file.is_file():
            err = f"Resolve scripting module not found at {mod_file} (is DaVinci Resolve installed? set RESOLVE_SCRIPT_API)"
        elif not Path(p["lib"]).is_file():
            err = f"fusionscript library not found at {p['lib']} (set RESOLVE_SCRIPT_LIB)"
        else:
            os.environ["RESOLVE_SCRIPT_LIB"] = p["lib"]        # DaVinciResolveScript reads it on import
            if p["modules"] not in sys.path:
                sys.path.append(p["modules"])
            try:
                mod = importlib.import_module(MODULE_NAME)
                if not hasattr(mod, "scriptapp"):
                    err = f"{MODULE_NAME} loaded but has no scriptapp() ({getattr(mod, '__file__', '?')})"
                else:
                    _module, _module_error = mod, None
                    logger.info("[Resolve] scripting module loaded modules=%s lib=%s", p["modules"], p["lib"])
                    return mod
            except (Exception, SystemExit) as e:         # the stock module raises ImportError / OSError
                sys.modules.pop(MODULE_NAME, None)
                err = f"Resolve scripting module failed to load: {type(e).__name__}: {e}"
        if err != _module_error:
            logger.warning("[Resolve] %s", err)
        _module_error, _module_error_at = err, time.monotonic()
        raise ResolveUnavailable(err)


def _in_thread(fn, timeout: float, what: str):
    """Run fn in a daemon thread; a hung call (Resolve starting / busy) is abandoned after
    ``timeout`` and no second one is started until it returns."""
    global _inflight
    with _inflight_lock:
        if _inflight is not None and _inflight.is_alive():
            raise ResolveNotRunning(msg("server.resolve.pending", "Resolve is not answering ({what} still pending -- is it starting up?)",
                                        what=what)["message"])
        box: dict = {}

        def work():
            try:
                box["r"] = fn()
            except BaseException as e:      # noqa: BLE001 -- reported to the caller
                box["e"] = e

        t = threading.Thread(target=work, daemon=True, name=f"resolve-{what}")
        t.start()
        t.join(timeout)
        if t.is_alive():
            _inflight = t
            logger.warning("[Resolve] %s timed out after %.1fs", what, timeout)
            raise ResolveNotRunning(_timeout_text(timeout))
        _inflight = None
    if "e" in box:
        e = box["e"]
        if isinstance(e, ResolveError):
            raise e
        raise ResolveError(f"Resolve {what} failed: {type(e).__name__}: {e}")
    return box.get("r")


NOT_RUNNING = msg("server.resolve.not_running", "DaVinci Resolve is not running or refuses scripting -- start DaVinci "
                  "Resolve, open the project, and check Preferences > System > General > External scripting using = "
                  "Local")["message"]


def _timeout_text(timeout: float) -> str:
    return msg("server.resolve.timeout", "Resolve did not answer within {seconds} s (starting up or busy?)",
               seconds=f"{timeout:.0f}")["message"]
# Resolve's internal script server can fail to start (e.g. when it comes up late on a busy machine): Resolve retries,
# gives up, and for the rest of that session nothing can script it although Resolve itself works. Its debug log says
# so; only a restart of Resolve brings scripting back.
SCRIPT_SERVER_DEAD = ("DaVinci Resolve is open but its scripting server failed to start (Resolve's log: 'Failed to "
                      "connect to script server') -- close DaVinci Resolve and open it again")


def debug_log_file() -> str:
    """Resolve's per-session debug log (rotated into LogArchive when Resolve starts)."""
    appdata = os.environ.get("APPDATA") or ""
    return os.path.join(appdata, "Blackmagic Design", "DaVinci Resolve", "Support", "logs", "ResolveDebug.txt")


def script_server_failed(path: str | None = None) -> bool:
    """Did this Resolve session give up on its script server? (The final 'Failed to connect to script server' --
    without ', retrying' -- comes after the last 'Started script server'.) Reads the log tail only, never a process."""
    try:
        with open(path or debug_log_file(), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 512 * 1024))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return False
    lines = text.splitlines()
    last_final = max((i for i, ln in enumerate(lines) if ln.strip() == "Failed to connect to script server"), default=-1)
    last_start = max((i for i, ln in enumerate(lines) if "Started script server" in ln), default=-1)
    return last_final >= 0 and last_final > last_start


# While a Resolve WE launched is starting, no probe may touch it until its own script server is up: probing a
# starting Resolve was observed to delay its script server by ~30 s and often make it fail, while an unprobed start
# brought it up in under a second. The hold ends when Resolve's debug log of THIS session says
# "Started script server", or after STARTUP_HOLD_S.
STARTUP_HOLD_S = 120.0
_hold = {"since": None}      # wall time of our launch; None = no hold


def hold_probes(t_wall: float) -> None:
    _hold["since"] = float(t_wall)
    invalidate_probe()


def release_probes() -> None:
    _hold["since"] = None


def _session_start(text: str):
    """Wall time of the 'Running DaVinci Resolve' line at the top of a debug log, or None."""
    import re
    m = re.search(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d+) \| Running DaVinci", text[:4096])
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp() + int(m.group(2)) / 1000.0
    except ValueError:
        return None


def session_log_present(path: str | None = None) -> bool:
    """ResolveDebug.txt exists only while a Resolve session runs (it is moved to LogArchive on exit)."""
    return os.path.isfile(path or debug_log_file())


def script_server_up_since(t_wall: float, path: str | None = None) -> bool:
    """Has the Resolve session started after t_wall started its script server? Reads the log only, never a process.
    The log must have been written after t_wall, and its first line ('Running DaVinci', naive local time) must not be
    older than t_wall by more than an hour (the DST fall-back hour makes a local stamp read 1 h early)."""
    p = path or debug_log_file()
    try:
        if os.stat(p).st_mtime < t_wall - 5.0:
            return False
        with open(p, "rb") as f:
            text = f.read(4 * 1024 * 1024).decode("utf-8", "replace")
    except OSError:
        return False
    start = _session_start(text)
    return start is not None and start >= t_wall - 3605.0 and "Started script server" in text


def starting_session(path: str | None = None, now: float | None = None) -> bool:
    """A Resolve session (anyone's launch: a manual one too) whose debug log exists, began less than STARTUP_HOLD_S ago
    and has no 'Started script server' yet (a single status poll landing in that window can cost Resolve a script
    server retry)."""
    p = path or debug_log_file()
    now = time.time() if now is None else now
    try:
        if now - os.stat(p).st_mtime > STARTUP_HOLD_S:
            return False
        with open(p, "rb") as f:
            text = f.read(4 * 1024 * 1024).decode("utf-8", "replace")
    except OSError:
        return False
    start = _session_start(text)
    return start is not None and now - start < STARTUP_HOLD_S and "Started script server" not in text


def probes_held() -> bool:
    """True while a Resolve is still bringing up its script server: one we launched (see STARTUP_HOLD_S), or any
    session whose fresh debug log has no script server line yet."""
    t = _hold["since"]
    if t is not None:
        if time.time() - t > STARTUP_HOLD_S or script_server_up_since(t):
            release_probes()
        else:
            return True
    return starting_session()


STARTING = msg("server.resolve.starting", "Resolve is starting (waiting for its scripting server)")["message"]


def connect(timeout: float = CONNECT_TIMEOUT_S):
    """The Resolve object; raises ResolveUnavailable / ResolveNotRunning. The in-process
    scriptapp is only tried once the (non-blocking) probe saw Resolve answer."""
    mod = load_module()
    st = probe_cached()
    if not st.get("connected"):
        st = probe_cached(max_age=0.0)          # explicit action: do not trust a stale "down"
    if not st.get("connected"):
        raise ResolveNotRunning(st.get("error") or NOT_RUNNING)
    r = _in_thread(lambda: mod.scriptapp("Resolve"), timeout, "connect")
    if r is None:
        invalidate_probe()
        raise ResolveNotRunning(NOT_RUNNING)
    return r


def _call(obj, name: str, *args, default=None):
    """obj.name(*args), None-safe and exception-safe (the API returns None / False on failure)."""
    if obj is None:
        return default
    try:
        v = getattr(obj, name)(*args)
    except Exception:            # noqa: BLE001
        logger.debug("[Resolve] call failed name=%s", name, exc_info=True)
        return default
    return default if v is None else v


def _gather(r) -> dict:
    """Connection facts from a Resolve object (None = not running)."""
    if r is None:
        return {"connected": False, "error": NOT_RUNNING}
    proj = _call(_call(r, "GetProjectManager"), "GetCurrentProject")
    return {"connected": True, "product": _call(r, "GetProductName"), "version": _call(r, "GetVersionString"),
            "project": _call(proj, "GetName"), "timeline": _call(_call(proj, "GetCurrentTimeline"), "GetName"),
            "error": None}


# The child interpreter: argv = modules dir, fusionscript lib. Prints one marked JSON line.
_CHILD = r"""
import json, os, sys
mods, lib = sys.argv[1], sys.argv[2]
os.environ["RESOLVE_SCRIPT_LIB"] = lib
sys.path.append(mods)
def c(o, n, *a):
    if o is None:
        return None
    try:
        return getattr(o, n)(*a)
    except Exception:
        return None
try:
    import DaVinciResolveScript as m
    r = m.scriptapp("Resolve")
    if r is None:
        out = {"connected": False}
    else:
        p = c(c(r, "GetProjectManager"), "GetCurrentProject")
        out = {"connected": True, "product": c(r, "GetProductName"), "version": c(r, "GetVersionString"),
               "project": c(p, "GetName"), "timeline": c(c(p, "GetCurrentTimeline"), "GetName")}
except BaseException as e:
    out = {"connected": False, "error": "%s: %s" % (type(e).__name__, e)}
sys.stdout.write("\nWCLIPS_PROBE " + json.dumps(out) + "\n")
sys.stdout.flush()
"""


def probe_subprocess(timeout: float | None = None) -> dict:
    """Ask a child interpreter whether Resolve answers (fusionscript holds the GIL while it
    tries, so the server never calls scriptapp blind). The child is killed on timeout."""
    p = script_paths()
    timeout = float(timeout or PROBE_TIMEOUT_S)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    t0 = time.monotonic()
    try:
        cp = subprocess.run([paths.python_exe(), "-I", "-c", _CHILD, p["modules"], p["lib"]], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=flags)
    except subprocess.TimeoutExpired:
        logger.warning("[Resolve] probe timed out after %.1fs", timeout)
        return {"connected": False, "error": _timeout_text(timeout)}
    except OSError as e:
        return {"connected": False, "error": f"probe could not start: {e}"}
    line = next((ln for ln in reversed(cp.stdout.splitlines()) if ln.startswith("WCLIPS_PROBE ")), None)
    if line is None:
        tail = (cp.stderr or cp.stdout or "").strip().splitlines()[-1:] or ["no output"]
        return {"connected": False, "error": f"probe failed rc={cp.returncode}: {tail[0][:200]}"}
    try:
        res = json.loads(line[len("WCLIPS_PROBE "):])
    except json.JSONDecodeError:
        return {"connected": False, "error": "probe answered garbage"}
    if not res.get("connected") and not res.get("error"):
        res["error"] = SCRIPT_SERVER_DEAD if script_server_failed() else NOT_RUNNING
    res.setdefault("error", None)
    logger.debug("[Resolve] probe connected=%s project=%s ms=%.0f", res.get("connected"), res.get("project"),
                 (time.monotonic() - t0) * 1000)
    return res


def probe_inprocess(timeout: float | None = None) -> dict:
    """Same answer from this process (thread + timeout). Only safe when scriptapp releases the
    GIL -- the tests' fake object model; the real fusionscript does not."""
    mod = load_module()
    try:
        return _in_thread(lambda: _gather(mod.scriptapp("Resolve")), float(timeout or CONNECT_TIMEOUT_S), "status")
    except ResolveError as e:
        return {"connected": False, "error": str(e)}


probe = probe_subprocess        # swapped for probe_inprocess by the tests


def invalidate_probe() -> None:
    global _probe_cache
    _probe_cache = None


def probe_cached(max_age: float | None = None, timeout: float | None = None) -> dict:
    """The last probe if younger than max_age (default PROBE_TTL_S); one probe at a time, so a
    polling UI never stacks child processes."""
    global _probe_cache
    if probes_held():                                   # our fresh launch: hands off until its script server is up
        return {"connected": False, "error": STARTING, "held": True}
    with _probe_lock:
        c = _probe_cache
        age = max_age
        if age is None:
            age = PROBE_TTL_S if (c is None or c[1].get("connected")) else PROBE_TTL_DOWN_S
        if c is not None and time.monotonic() - c[0] < age:
            return c[1]
        try:
            res = probe(timeout)
        except ResolveError as e:
            res = {"connected": False, "error": str(e)}
        prev = c[1] if c else None
        if prev is None or prev.get("connected") != res.get("connected") or prev.get("project") != res.get("project"):
            logger.info("[Resolve] status connected=%s project=%s error=%s", res.get("connected"), res.get("project"),
                        res.get("error"))
        _probe_cache = (time.monotonic(), res)
        return res


def status(fresh: bool = False, timeout: float | None = None) -> dict:
    """{available, connected, product, version, project, timeline, error, paths}; never raises."""
    out = {"available": False, "connected": False, "product": None, "version": None, "project": None,
           "timeline": None, "error": None, "paths": script_paths()}
    try:
        load_module()
    except ResolveUnavailable as e:
        out["error"] = str(e)
        return out
    out["available"] = True
    res = probe_cached(max_age=0.0 if fresh else None, timeout=timeout)
    for k in ("connected", "product", "version", "project", "timeline", "error"):
        if k in res:
            out[k] = res[k]
    out["connected"] = bool(out["connected"])
    if out["connected"] and not out["project"]:
        out["error"] = msg("server.resolve.no_project_open", "no project open in Resolve")["message"]
    return out


@contextmanager
def _op(what: str):
    if not _op_lock.acquire(timeout=OP_WAIT_S):
        raise ResolveError(msg("server.resolve.handoff_busy", "another Resolve hand-off is still running ({what} not started)",
                               what=what)["message"])
    try:
        yield
    finally:
        _op_lock.release()


# ---- object-model helpers ---------------------------------------------------------------------
def norm_path(p) -> str:
    """Windows path compare key: separators unified, normalized, case-folded."""
    s = str(p or "").strip().strip('"').replace("/", "\\")
    return ntpath.normpath(s).lower() if s else ""


def _project(resolve):
    proj = _call(_call(resolve, "GetProjectManager"), "GetCurrentProject")
    if not proj:
        raise ResolveError(msg("server.resolve.no_project_hint",
                               "no project open in Resolve -- open the project you are cutting first")["message"])
    mp = _call(proj, "GetMediaPool")
    if not mp:
        raise ResolveError("the open project has no media pool (API refused)")
    return proj, mp


def walk_folders(folder):
    if folder is None:
        return
    stack = [folder]
    while stack:
        f = stack.pop()
        yield f
        subs = _call(f, "GetSubFolderList", default=[]) or []
        stack.extend(reversed(list(subs)))


class PoolIndex:
    """File path -> MediaPoolItem for every clip in the project's media pool (one walk)."""

    def __init__(self, media_pool):
        self.mp = media_pool
        self.by_path: dict[str, object] = {}
        self.by_name: dict[str, list] = {}
        self.where: dict[int, tuple] = {}       # id(item) -> (folder, "WardogClips/<date>"): the bin it sits in
        n = 0
        root = _call(media_pool, "GetRootFolder")
        stack = [(root, "")] if root is not None else []
        while stack:
            folder, label = stack.pop()
            for item in _call(folder, "GetClipList", default=[]) or []:
                fp = _call(item, "GetClipProperty", "File Path")
                if not fp or not isinstance(fp, str):
                    continue                    # timelines, compound clips, generators
                key = norm_path(fp)
                self.by_path.setdefault(key, item)
                self.by_name.setdefault(ntpath.basename(key), []).append(item)
                self.where.setdefault(id(item), (folder, label or (_call(folder, "GetName") or "Master")))
                n += 1
            subs = list(_call(folder, "GetSubFolderList", default=[]) or [])
            for sub in reversed(subs):
                name = _call(sub, "GetName") or "?"
                stack.append((sub, f"{label}/{name}" if label else name))
        self.n = n

    def find(self, path: str):
        """(item, how): exact path first; else a unique clip with the same file name (OBS names
        are timestamps, so that is the same recording after a move to another drive)."""
        key = norm_path(path)
        item = self.by_path.get(key)
        if item is not None:
            return item, "path"
        same = self.by_name.get(ntpath.basename(key)) or []
        if len(same) == 1:
            return same[0], "name"
        return None, None

    def add(self, path: str, item, folder=None, label: str | None = None) -> None:
        key = norm_path(path)
        self.by_path[key] = item
        self.by_name.setdefault(ntpath.basename(key), []).append(item)
        if folder is not None:
            self.where[id(item)] = (folder, label or "")


def ensure_folder(media_pool, parent, name: str):
    for sub in _call(parent, "GetSubFolderList", default=[]) or []:
        if _call(sub, "GetName") == name:
            return sub
    f = _call(media_pool, "AddSubFolder", parent, name)
    if not f:
        raise ResolveError(f"could not create media pool bin '{name}'")
    return f


def import_item(media_pool, path: str, bin_name: str, date: str, where: dict | None = None):
    """ImportMedia into <bin>/<date>; the current folder is restored afterwards. `where` (optional) receives
    {"folder", "label"} = the bin it went into."""
    if not os.path.exists(path):
        raise ResolveError(f"file not found (offline drive?): {path}")
    root = _call(media_pool, "GetRootFolder")
    if root is None:
        raise ResolveError("media pool has no root folder (API refused)")
    saved = _call(media_pool, "GetCurrentFolder")
    try:
        target = ensure_folder(media_pool, ensure_folder(media_pool, root, bin_name or "WardogClips"), date)
        _call(media_pool, "SetCurrentFolder", target)
        items = _call(media_pool, "ImportMedia", [path], default=[]) or []
    finally:
        if saved is not None:
            _call(media_pool, "SetCurrentFolder", saved)
    if not items:
        raise ResolveError(f"Resolve refused to import {path}")
    logger.info("[Resolve] imported path=%s bin=%s/%s", path, bin_name, date)
    if where is not None:
        where.update(folder=target, label=f"{bin_name or 'WardogClips'}/{date}")
    return items[0]


def _item_fps(item, fallback: float) -> float:
    v = _call(item, "GetClipProperty", "FPS")
    try:
        f = float(str(v).split()[0])
        if f > 0:
            return f
    except (TypeError, ValueError, IndexError):
        pass
    return float(fallback or export.DEFAULT_FPS)


def _item_frames(item) -> int | None:
    v = _call(item, "GetClipProperty", "Frames")
    try:
        n = int(float(str(v).split()[0]))
        return n if n > 0 else None
    except (TypeError, ValueError, IndexError):
        return None


def marker_color(cfg, kind: str) -> str:
    k = (kind or "").lower()
    if k in MARKER_KINDS:
        return getattr(cfg.resolve, f"color_{k}", None) or cfg.resolve.color_manual
    return cfg.resolve.color_manual


def _resolve_items(mp, idx: PoolIndex, cfg, recs: list[dict]) -> tuple[dict, list, list]:
    """rec_id -> item for every recording; (items, imported paths, failures)."""
    items, imported, failed = {}, [], []
    for rec in recs:
        item, how = idx.find(rec["rec_path"])
        if item is None:
            where: dict = {}
            try:
                item = import_item(mp, rec["rec_path"], cfg.resolve.bin, rec["rec_date"], where=where)
            except ResolveError as e:
                failed.append({"rec_id": rec["rec_id"], "path": rec["rec_path"], "error": str(e)})
                continue
            idx.add(rec["rec_path"], item, where.get("folder"), where.get("label"))
            imported.append(rec["rec_path"])
            how = "imported"
        items[rec["rec_id"]] = item
        logger.debug("[Resolve] item rec=%s how=%s", rec["rec_id"], how)
    return items, imported, failed


def _recs_of(rows: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in rows:
        if r["rec_id"] not in seen:
            seen.add(r["rec_id"])
            out.append({"rec_id": r["rec_id"], "rec_path": r["rec_path"], "rec_date": r["rec_date"]})
    return out


# ---- markers -----------------------------------------------------------------------------------
def send_markers(db, cfg, events: list[dict], resolve=None) -> dict:
    """One marker per event on its recording's MediaPoolItem; idempotent by customData."""
    if not events:
        raise export.SelectionError("no events selected")
    t0 = time.monotonic()
    with _op("markers"):
        resolve = resolve or connect()
        proj, mp = _project(resolve)
        idx = PoolIndex(mp)
        items, imported, failed = _resolve_items(mp, idx, cfg, _recs_of(events))
        failed_recs = {f["rec_id"]: f["error"] for f in failed}
        sent, failures = [], []
        by_rec: dict[int, list[dict]] = {}
        for ev in events:
            by_rec.setdefault(ev["rec_id"], []).append(ev)
        for rec_id, evs in by_rec.items():
            item = items.get(rec_id)
            if item is None:
                failures += [{"event_id": e["id"], "error": failed_recs.get(rec_id, "no media pool item")} for e in evs]
                continue
            fps = _item_fps(item, evs[0]["fps"])
            total = _item_frames(item)
            for ev in evs:                      # idempotency: drop what we made before
                for cd in {f"{CUSTOM_PREFIX}{ev['id']}", ev.get("resolve_marker") or ""}:
                    if cd.startswith(CUSTOM_PREFIX):
                        _call(item, "DeleteMarkerByCustomData", cd)
            occupied = set()
            for k in (_call(item, "GetMarkers", default={}) or {}):
                try:
                    occupied.add(int(float(k)))
                except (TypeError, ValueError):
                    pass
            for ev in sorted(evs, key=lambda e: (e["clip_in_s"], e["t_s"])):
                cd = f"{CUSTOM_PREFIX}{ev['id']}"
                frame = export.frames_at(ev["clip_in_s"], fps)
                dur = max(1, export.frames_at(ev["clip_out_s"], fps) - frame)
                if total:
                    frame = max(0, min(frame, total - 1))
                name = export.marker_name(ev)
                note = export.marker_note(ev, export.transcript_near(db, ev["rec_id"], float(ev["t_s"])))
                color = marker_color(cfg, ev["kind"])
                ok = False
                d = dur
                for _ in range(240):            # one marker per frame: nudge forward past collisions
                    while frame in occupied:
                        frame += 1
                    if total and frame >= total:
                        break
                    d = max(1, min(dur, total - frame) if total else dur)
                    if _call(item, "AddMarker", frame, color, name, note, d, cd, default=False):
                        ok = True
                        break
                    occupied.add(frame)         # refused: treat as taken, try the next frame
                if not ok:
                    failures.append({"event_id": ev["id"], "error": f"AddMarker refused near frame {frame}"})
                    continue
                occupied.add(frame)
                db.execute("UPDATE events SET resolve_marker=? WHERE id=?", (cd, ev["id"]))
                sent.append({"event_id": ev["id"], "rec_id": rec_id, "frame": frame, "duration": d, "fps": fps,
                             "color": color, "name": name})
        project = _call(proj, "GetName")
        # show where the clips are (looking at the bin of an earlier send -- each recording goes
        # to <bin>/<its date> -- shows "no clip"): the media pool opens on the bin of the last clip marked
        bins = []
        for rec_id in dict.fromkeys(m["rec_id"] for m in sent):
            w = idx.where.get(id(items[rec_id]))
            if w and w[1] not in bins:
                bins.append(w[1])
        if sent:
            w = idx.where.get(id(items[sent[-1]["rec_id"]]))
            if w and w[0] is not None:
                _call(mp, "SetCurrentFolder", w[0])
    logger.info("[Resolve] markers project=%s sent=%d failed=%d imported=%d bins=%s ms=%.0f", project, len(sent),
                len(failures), len(imported), ",".join(bins), (time.monotonic() - t0) * 1000)
    return {"ok": not failures, "project": project, "sent": len(sent), "markers": sent, "failed": failures,
            "imported": imported, "recordings": len(by_rec), "bins": bins}


# ---- selects timeline --------------------------------------------------------------------------
def _timeline_names(proj) -> set[str]:
    names = set()
    n = int(_call(proj, "GetTimelineCount", default=0) or 0)
    for i in range(1, n + 1):
        nm = _call(_call(proj, "GetTimelineByIndex", i), "GetName")
        if nm:
            names.add(nm)
    return names


def unique_name(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base} {n}" in taken:
        n += 1
    return f"{base} {n}"


def build_selects(db, cfg, events: list[dict], order: str = "score", name: str | None = None, resolve=None) -> dict:
    """A new timeline: merged clip ranges appended in score / time order, a marker per clip."""
    if not events:
        raise export.SelectionError("no events selected")
    clips = export.merge_clips(events, order)
    t0 = time.monotonic()
    with _op("selects"):
        resolve = resolve or connect()
        proj, mp = _project(resolve)
        idx = PoolIndex(mp)
        items, imported, failed = _resolve_items(mp, idx, cfg, _recs_of(events))
        if not items:                   # nothing placeable: do not leave an empty timeline behind
            raise ResolveError("no clip could be found or imported in Resolve -- " + "; ".join(
                f["error"] for f in failed[:3]))
        base = (name or cfg.resolve.selects_name or "Selects {date}").replace("{date}", datetime.now().strftime("%Y-%m-%d"))
        tl_name = unique_name(base.strip() or "Selects", _timeline_names(proj))
        root = _call(mp, "GetRootFolder")
        saved = _call(mp, "GetCurrentFolder")
        try:
            if root is not None:
                _call(mp, "SetCurrentFolder", ensure_folder(mp, root, cfg.resolve.bin or "WardogClips"))
            tl = _call(mp, "CreateEmptyTimeline", tl_name)
        finally:
            if saved is not None:
                _call(mp, "SetCurrentFolder", saved)
        if not tl:
            raise ResolveError(f"Resolve refused to create timeline '{tl_name}'")
        _call(proj, "SetCurrentTimeline", tl)
        try:
            tl_fps = float(_call(tl, "GetSetting", "timelineFrameRate") or _call(proj, "GetSetting", "timelineFrameRate")
                           or export.DEFAULT_FPS)
        except (TypeError, ValueError):
            tl_fps = export.DEFAULT_FPS
        tl_start = int(_call(tl, "GetStartFrame", default=0) or 0)
        pos = 0
        placed, failures = [], []
        for c in clips:
            item = items.get(c["rec_id"])
            if item is None:
                failures.append({"event_ids": c["event_ids"], "error": "no media pool item for " + c["rec_name"]})
                continue
            fps = _item_fps(item, c["fps"])
            total = _item_frames(item)
            s = export.frames_at(c["clip_in_s"], fps)
            e = max(s, export.frames_at(c["clip_out_s"], fps) - 1)       # endFrame is the last frame
            if total:
                s, e = min(s, total - 1), min(e, total - 1)
            res = _call(mp, "AppendToTimeline", [{"mediaPoolItem": item, "startFrame": s, "endFrame": e}], default=[])
            if not res:
                failures.append({"event_ids": c["event_ids"], "error": "AppendToTimeline refused"})
                continue
            ti = res[0] if isinstance(res, (list, tuple)) else res
            rel, dur = pos, max(1, int(round((e - s + 1) * tl_fps / fps)))
            st, du = _call(ti, "GetStart"), _call(ti, "GetDuration")
            if isinstance(st, (int, float)) and isinstance(du, (int, float)) and du > 0:
                rel, dur = int(st) - tl_start, int(du)
            evs = c["events"]
            top = max(evs, key=lambda x: float(x["score"] or 0.0))
            mname = " + ".join(export.marker_name(x) for x in evs)[:120]
            tr = export.transcript_near(db, c["rec_id"], float(top["t_s"]))
            note = " || ".join(export.marker_note(x) for x in evs)
            if tr:
                note += ' | voice: "' + tr + '"'
            _call(tl, "AddMarker", rel, marker_color(cfg, top["kind"]), mname, note[:1500], dur,
                  f"{CUSTOM_PREFIX}sel:{','.join(str(i) for i in c['event_ids'])}")
            placed.append({"event_ids": c["event_ids"], "rec_id": c["rec_id"], "start_frame": s, "end_frame": e,
                           "record_frame": rel, "duration": dur, "name": mname})
            pos = rel + dur
        project = _call(proj, "GetName")
    logger.info("[Resolve] selects project=%s timeline=%s clips=%d events=%d failed=%d ms=%.0f", project, tl_name,
                len(placed), len(events), len(failures), (time.monotonic() - t0) * 1000)
    return {"ok": not failures and not failed, "project": project, "timeline": tl_name, "clips": len(placed),
            "events": len(events), "placed": placed, "failed": failures + failed, "imported": imported}
