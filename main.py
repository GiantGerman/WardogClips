"""WardogClips entry point.

Two ways to run:
- console (`python main.py`): banner on the console, closing the window or Ctrl+C
  stops the program (running stages are checkpointed per job).
- tray (`pythonw.exe main.py --tray`, what the installed WardogClips.exe starts): no console window. Logging goes
  to the file only, uvicorn serves on a worker thread and the tray icon (desktop/tray.py) owns the main thread;
  its Quit and POST /api/system/quit run the same shutdown. Without pystray it falls back to the console path.

Single instance: an exclusive lock on `<data dir>\\instance.lock`, held for the process life and
taken before any port probing; a launch that cannot get it waits up to 20 s for the holder to answer, opens the
browser there and exits 0 (--scan takes the lock too, or refuses with exit 2). Then `<data dir>\\instance.json`
({pid, port, version, started, started_epoch, data_dir}, written once uvicorn listens, removed on a clean stop) and
the configured port are asked for /api/status; a WardogClips with the SAME data dir answering
-> this launch opens the browser there and exits 0. Port: server.port (8558) or WARDOGCLIPS_PORT; held by anything
else (another program, or another WardogClips copy with its own data dir) -> the next free port up to +20.

Ban safety: this process never opens, reads, injects into, or signals the game. It reads
the recording files the library points at, read-only, and writes only under the data dir.
"""

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser

logger = logging.getLogger("wclips.main")

PORT_TRIES = 20                 # server.port busy -> port+1 .. port+20
GRACEFUL_S = 3                  # uvicorn: open connections (the page's SSE stream) get this long on shutdown
SHUTDOWN_WAIT_S = 90            # a second way out waits at most this long for the first shutdown to finish
FORCE_WAIT_S = 20               # console: after the 2nd Ctrl+C the stop gets this long before the process exits anyway
CLOSE_STOP_S = 4.0              # console window closed: Windows ends the process ~5 s after CTRL_CLOSE_EVENT
CREATE_NO_WINDOW = 0x08000000


# ---- pythonw / console safety --------------------------------------------------------------------------------------
def _fix_missing_streams() -> bool:
    """Under pythonw.exe sys.stdout / sys.stderr are None and any print() or a library's progress bar (tqdm in the
    Hugging Face download, uvicorn's default handlers) raises. Point them at os.devnull. Returns True when there was
    no console (= the caller must not attach a console log handler)."""
    missing = sys.stdout is None or sys.stderr is None
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")        # noqa: SIM115 - lives as long as the process
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")        # noqa: SIM115
    return missing


def install_no_window_popen() -> bool:
    """Tray-mode safety net (Windows): a child started WITHOUT creationflags gets CREATE_NO_WINDOW, so a forgotten
    call (ffmpeg, nvidia-smi, taskkill ...) never flashes a console over a running game. A call that passes its own
    flags (the explorer.exe / DETACHED Resolve launch in resolve/link.py) is left exactly as it is. Idempotent."""
    if os.name != "nt" or getattr(subprocess.Popen, "_wclips_no_window", False):
        return False
    orig = subprocess.Popen.__init__

    def __init__(self, *args, **kwargs):
        # creationflags is the 14th positional parameter; nobody passes it that way, but never override it if so
        if not kwargs.get("creationflags") and len(args) < 14:
            kwargs["creationflags"] = CREATE_NO_WINDOW
        orig(self, *args, **kwargs)
    subprocess.Popen.__init__ = __init__
    subprocess.Popen._wclips_no_window = True
    return True


# ---- single instance / port ------------------------------------------------------------------------------------------
def _same_dir(a, b) -> bool:
    try:
        return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))
    except (TypeError, ValueError):
        return False


def _status(host: str, port: int, timeout: float = 2.0) -> dict | None:
    import requests
    h = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    try:
        r = requests.get(f"http://{h}:{port}/api/status", timeout=timeout)
        js = r.json() if r.ok else None
        return js if isinstance(js, dict) else None
    except Exception:  # noqa: BLE001 - nothing there / not JSON
        return None


def _already_running(host: str, port: int) -> bool:
    """A WardogClips on host:port that uses OUR data dir (another copy or install with its own data dir is a
    different app as far as this launch is concerned: it gets the next free port instead of being reopened)."""
    from utils import paths
    st = _status(host, port)
    if not st or st.get("app") != "WardogClips":
        return False
    dd = st.get("data_dir")
    return dd is None or _same_dir(dd, paths.DATA_DIR)


LOCK_WAIT_S = 20.0              # a launch that lost the data-dir lock waits this long for the winner to answer
LOCK_INFO_AT = 16               # the lock covers byte 0; {pid, mode} is written from here (readable while locked)
_lock_fd: int | None = None     # held open (= locked) for the whole process life; the OS drops it on any exit


def _lock_byte0(fd: int) -> bool:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 0)
        return True
    except OSError:
        return False


def _unlock_byte0(fd: int) -> None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.lockf(fd, fcntl.LOCK_UN, 1, 0)
    except OSError:
        pass


def acquire_instance_lock(mode: str) -> bool:
    """Exclusive per-data-dir lock on <data dir>\\instance.lock, taken BEFORE any port probing or binding. Two
    launches at once (a double-click on the shortcut while the first is still building its context, ~3 s) would
    otherwise both see 'nothing answers' and start two servers on one DB. A crash or a
    kill releases the lock with the process, so it cannot go stale. --scan takes it too: a headless scan next to a
    running app is the same two-job-lanes-on-one-DB problem. True = we hold it now (idempotent)."""
    global _lock_fd
    if _lock_fd is not None:
        return True
    from utils import paths
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = paths.DATA_DIR / "instance.lock"
    try:
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0),
                     0o644)
    except OSError:
        return False
    if not _lock_byte0(fd):
        os.close(fd)
        return False
    try:
        info = json.dumps({"pid": os.getpid(), "mode": mode}).encode("ascii") + b"\n"
        os.lseek(fd, LOCK_INFO_AT, os.SEEK_SET)
        os.write(fd, info)
        os.ftruncate(fd, LOCK_INFO_AT + len(info))
    except OSError:
        pass
    _lock_fd = fd
    return True


def release_instance_lock() -> None:
    """Tests only (the process exit releases it in the app)."""
    global _lock_fd
    if _lock_fd is not None:
        _unlock_byte0(_lock_fd)
        try:
            os.close(_lock_fd)
        except OSError:
            pass
        _lock_fd = None


def lock_holder() -> dict | None:
    """{pid, mode} of the process holding the data-dir lock (written past the locked byte, so readable), or None
    when nobody holds it."""
    from utils import paths
    lock = paths.DATA_DIR / "instance.lock"
    if _lock_fd is not None or not lock.exists():
        return None
    try:
        fd = os.open(str(lock), os.O_RDWR | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    try:
        if _lock_byte0(fd):            # free: nobody holds it -- let go of the probe lock at once
            _unlock_byte0(fd)
            return None
        os.lseek(fd, LOCK_INFO_AT, os.SEEK_SET)
        raw = os.read(fd, 512)
        try:
            js = json.loads(raw.decode("ascii", "replace").strip() or "{}")
        except ValueError:
            js = {}
        return js if isinstance(js, dict) else {}
    except OSError:
        return {}
    finally:
        os.close(fd)


def wait_for_winner(host: str, timeout: float = LOCK_WAIT_S) -> int | None:
    """The data-dir lock is held: wait for that instance to answer (its instance.json + /api/status with our data
    dir) -> its port, or None after `timeout` (or at once when the holder is a headless scan, which never serves)."""
    t0 = time.monotonic()
    while True:
        holder = lock_holder()
        if holder is None:
            return None                 # the holder went away meanwhile
        if holder.get("mode") == "scan":
            return None
        inst = read_instance()
        if inst is not None and _already_running(host, int(inst["port"])):
            return int(inst["port"])
        if time.monotonic() - t0 >= timeout:
            return None
        time.sleep(0.25)


def read_instance() -> dict | None:
    from utils import paths
    try:
        js = json.loads(paths.INSTANCE_FILE.read_text(encoding="utf-8"))
        return js if isinstance(js, dict) and int(js.get("port") or 0) > 0 else None
    except (OSError, ValueError, TypeError):
        return None


def write_instance(port: int) -> None:
    """Written once uvicorn listens (tmp + replace: a reader never sees half a file)."""
    from utils import paths
    data = {"pid": os.getpid(), "port": int(port), "version": paths.APP_VERSION,
            "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "started_epoch": round(time.time(), 1),
            "data_dir": str(paths.DATA_DIR)}
    tmp = paths.INSTANCE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, paths.INSTANCE_FILE)
    logger.info("[Main] instance file written port=%d pid=%d", port, os.getpid())


def remove_instance() -> None:
    """Only OUR instance.json (a second process that lost the race must not delete the winner's)."""
    from utils import paths
    cur = read_instance()
    if cur is not None and int(cur.get("pid") or 0) not in (0, os.getpid()):
        return
    try:
        paths.INSTANCE_FILE.unlink()
        logger.info("[Main] instance file removed")
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("[Main] instance file not removed err=%s", e)


def port_free(host: str, port: int) -> bool:
    """Can we bind host:port right now? SO_EXCLUSIVEADDRUSE on Windows: without it a bind can 'succeed' on a port
    another process listens on with SO_REUSEADDR."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind((host or "127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def claim_port(host: str, port: int, tries: int = PORT_TRIES) -> tuple[socket.socket | None, int | None]:
    """Bind + listen on the first free port in port .. port+tries with SO_EXCLUSIVEADDRUSE and hand that socket to
    uvicorn. uvicorn's own bind sets SO_REUSEADDR, and on Windows that lets a SECOND server bind a port the first is
    already serving (requests then go to either). An exclusive socket can
    neither be shared nor be stolen."""
    for p in range(port, port + tries + 1):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind((host or "127.0.0.1", p))
            s.listen(128)
            s.set_inheritable(False)
            return s, p
        except OSError:
            s.close()
    return None, None


def configured_port(cfg) -> int:
    env = os.environ.get("WARDOGCLIPS_PORT", "").strip()
    if env:
        try:
            p = int(env)
            if 1024 <= p <= 65535:
                return p
            logger.warning("[Main] WARDOGCLIPS_PORT=%s out of range -- using server.port", env)
        except ValueError:
            logger.warning("[Main] WARDOGCLIPS_PORT=%s is not a number -- using server.port", env)
    return int(cfg.server.port)


def find_instance_or_port(host: str, port: int, tries: int = PORT_TRIES) -> tuple[int | None, int | None]:
    """-> (running_port, None) when our instance already serves, else (None, free_port) or (None, None) when
    port .. port+tries are all held."""
    inst = read_instance()
    if inst is not None:
        if _already_running(host, int(inst["port"])):
            return int(inst["port"]), None
        logger.info("[Main] stale instance file port=%s pid=%s -- ignored", inst.get("port"), inst.get("pid"))
    if _already_running(host, port):
        return port, None
    for p in range(port, port + tries + 1):
        if port_free(host, p):
            if p != port:
                logger.info("[Main] port %d busy -- using %d", port, p)
            return None, p
    return None, None


# ---- the server ------------------------------------------------------------------------------------------------------
class Runner:
    """uvicorn on a worker thread + one idempotent shutdown for every way out (tray Quit, POST /api/system/quit,
    Ctrl+C, the server dying): should_exit -> join the server -> ctx.stop() -> remove instance.json -> on_stopped
    (the tray icon goes last)."""

    def __init__(self, ctx, app, host: str, port: int, tray: bool, sock: socket.socket | None = None):
        import uvicorn
        self.ctx, self.host, self.port, self.sock = ctx, host, port, sock
        # tray: no uvicorn log config (its default handlers write to the console we do not have); its loggers then
        # propagate to our file handler, capped at WARNING by logging_config
        kw = {"log_config": None} if tray else {}
        # an open page holds the SSE stream (/api/stream) forever: without a graceful-shutdown limit uvicorn waits
        # for it and Quit takes ~20 s; after GRACEFUL_S the remaining connections are canceled
        self.server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning",
                                                    timeout_graceful_shutdown=GRACEFUL_S, **kw))
        self.thread = threading.Thread(target=self._serve, name="uvicorn", daemon=True)
        self.on_stopped = None
        self._lock = threading.Lock()
        self._stopping = False
        self._stopper: threading.Thread | None = None     # the thread running the first shutdown()
        self.stopped = threading.Event()
        self._ctrl_handler = None                          # kept referenced: Windows calls it from its own thread
        self.error: str | None = None
        logging.getLogger("uvicorn.error").addFilter(self._quiet_cancel)

    def _quiet_cancel(self, record: logging.LogRecord) -> bool:
        """Our own shutdown cancels the page's SSE stream after GRACEFUL_S: uvicorn logs that as two ERRORs with a
        CancelledError traceback. Expected while stopping -> dropped (one INFO line instead)."""
        if not self._stopping:
            return True
        msg = record.getMessage()
        if msg.startswith("Cancel ") and "graceful shutdown" in msg:
            logger.info("[Main] open connections canceled after %ds (the page's live stream)", GRACEFUL_S)
            return False
        exc = record.exc_info[1] if record.exc_info else None
        import asyncio
        return not (msg.startswith("Exception in ASGI application") and isinstance(exc, asyncio.CancelledError))

    def _serve(self):
        try:
            self.server.run(sockets=[self.sock] if self.sock is not None else None)
        except BaseException as e:  # noqa: BLE001 - SystemExit from a failed bind lands here too
            self.error = f"{type(e).__name__}: {e}"
            logger.error("[Main] server stopped with an error err=%s", self.error)
        finally:
            if not self.server.started:
                self.error = self.error or "the server did not start (port taken?)"

    def start(self, timeout: float = 30.0) -> bool:
        """Start serving; True once uvicorn listens (then instance.json is written)."""
        self.thread.start()
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if self.server.started:
                if not self._stopping:       # a quit that raced the start: no instance file left behind
                    write_instance(self.port)
                return True
            if not self.thread.is_alive():
                return False
            time.sleep(0.05)
        return False

    def shutdown(self, reason: str = "quit") -> None:
        with self._lock:
            if self._stopping:
                busy = True
            else:
                busy, self._stopping = False, True
                self._stopper = threading.current_thread()
        if busy:
            # a second way out while the first is still stopping (tray ended + HTTP quit): wait for it, so the
            # main thread never exits under a half-done ctx.stop() -- but only while that first caller is alive
            # (a shutdown aborted by a 2nd Ctrl+C never sets `stopped`, which would otherwise hang here)
            t0 = time.monotonic()
            while not self.stopped.wait(0.5):
                first = self._stopper
                if first is None or not first.is_alive() or time.monotonic() - t0 > SHUTDOWN_WAIT_S:
                    break
            return
        logger.info("[Main] shutting down reason=%s", reason)
        self.server.should_exit = True
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            logger.warning("[Main] server did not stop in 15 s -- forcing")
            self.server.force_exit = True
            self.thread.join(timeout=5)
        try:
            self.ctx.stop()
        except Exception:  # noqa: BLE001 - the instance file must still go
            logger.exception("[Main] ctx.stop failed")
        remove_instance()
        self.stopped.set()
        if self.on_stopped is not None:
            try:
                self.on_stopped()
            except Exception:  # noqa: BLE001
                logger.exception("[Main] on_stopped failed")

    def wait_console(self) -> None:
        """Console mode: block until the server ends; Ctrl+C = shutdown (twice = force).

        The shutdown runs on a worker thread and this (main) thread only waits: a second Ctrl+C raises
        KeyboardInterrupt HERE, not inside ctx.stop(), so the stop always completes (otherwise it could abort
        shutdown halfway -- lanes never told to stop, instance.json left behind). Second Ctrl+C = cut the
        open connections and keep waiting up to FORCE_WAIT_S; a third (or that limit) = give up waiting: the
        instance file goes and the process exits."""
        try:
            while self.thread.is_alive():
                self.thread.join(timeout=0.5)
        except KeyboardInterrupt:
            logger.info("[Main] Ctrl+C -- stopping (again to force)")
        worker = threading.Thread(target=self.shutdown, args=("ctrl+c" if self.thread.is_alive() else "server ended",),
                                  name="shutdown", daemon=True)
        worker.start()
        presses, deadline = 0, None
        while not self.stopped.is_set():
            try:
                if self.stopped.wait(0.5):
                    break
                if deadline is not None and time.monotonic() > deadline:
                    logger.warning("[Main] shutdown still busy after %d s -- exiting anyway", int(FORCE_WAIT_S))
                    break
                if not worker.is_alive():
                    break
            except KeyboardInterrupt:
                presses += 1
                if presses == 1:
                    logger.info("[Main] Ctrl+C again -- forcing the server down (once more to exit at once)")
                    self.server.force_exit = True
                    deadline = time.monotonic() + FORCE_WAIT_S
                else:
                    logger.warning("[Main] Ctrl+C a third time -- exiting without waiting for the stop")
                    break
        if not self.stopped.is_set():
            remove_instance()

    def install_console_close_handler(self) -> bool:
        """Closing the console window (the banner's 'CLOSE THIS WINDOW'), logoff and shutdown would otherwise skip
        every bit of cleanup. Windows gives a CTRL_CLOSE handler about 5 s before it ends the process: remove
        instance.json first (cheap), then run the normal shutdown time-boxed. Ctrl+C / Ctrl+Break pass through
        (False) to Python's own KeyboardInterrupt."""
        if os.name != "nt":
            return False
        try:
            import ctypes
            from ctypes import wintypes
            HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

            def _on_ctrl(event: int) -> bool:
                if event not in (2, 5, 6):             # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
                    return False
                try:
                    logger.info("[Main] console closing event=%d -- stopping", event)
                    remove_instance()
                    t = threading.Thread(target=self.shutdown, args=("console closed",), name="close-stop",
                                         daemon=True)
                    t.start()
                    self.server.force_exit = True      # no 3 s grace for the page's stream: the clock is ticking
                    t.join(CLOSE_STOP_S)
                except Exception:  # noqa: BLE001 - never raise into Windows' handler thread
                    pass
                return True
            self._ctrl_handler = HANDLER(_on_ctrl)
            return bool(ctypes.windll.kernel32.SetConsoleCtrlHandler(self._ctrl_handler, True))
        except Exception as e:  # noqa: BLE001
            logger.warning("[Main] console close handler not installed err=%s", e)
            return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="WardogClips")
    ap.add_argument("--debug", action="store_true", help="DEBUG-level file AND console logging")
    ap.add_argument("--verbose", action="store_true", help="DEBUG lines on the console")
    ap.add_argument("--no-browser", action="store_true", help="do not open the UI")
    ap.add_argument("--tray", action="store_true", help="no console: a system-tray icon (Open / Quit); the installed"
                                                        " app starts this way under pythonw.exe")
    ap.add_argument("--scan", nargs="?", const="new", metavar="new|all|transcribe|transcribe-all|detect|full",
                    help="headless: scan the library and run the queued probes on this thread, then exit;"
                         " 'transcribe' / 'detect' also run that stage on every probed recording that lacks it"
                         " (transcribe: also the ones made before the mic pass), 'transcribe-all' re-transcribes"
                         " every recording, 'full' runs transcribe + detect")
    ap.add_argument("--check-running", action="store_true",
                    help="print the port of the WardogClips serving THIS data dir and exit 0 (waits up to 20 s for"
                         " one that is still starting); exit 1 (nothing printed) when none runs; 'scan' + exit 2"
                         " when a headless scan holds the data dir. Used by start scripts to reopen a running copy.")
    args = ap.parse_args(argv)

    no_console = _fix_missing_streams()
    tray = bool(args.tray) and not args.scan
    from utils import paths
    if args.check_running:
        return _check_running()
    # one process per data dir, decided BEFORE logging opens the shared log file and before any port probing
    have_lock = acquire_instance_lock("scan" if args.scan else ("tray" if tray else "console"))
    from utils.logging_config import setup_logging
    setup_logging(debug=args.debug, console=not (tray or no_console))
    if not have_lock:
        r = _lost_lock(args)
        if r is not None:
            return r
    logger.info("[Main] WardogClips v%s starting data_dir=%s (%s) mode=%s installed=%s", paths.APP_VERSION,
                paths.DATA_DIR, paths.DATA_DIR_REASON, "tray" if tray else ("scan" if args.scan else "console"),
                paths.INSTALLED)
    if tray and install_no_window_popen():
        logger.info("[Main] child processes start without a console window (tray mode)")
    from utils import ortdml
    logger.info("[Main] onnxruntime: %s", ortdml.activate())     # before anything imports onnxruntime (GPU OCR)
    try:
        from core import packs                                     # before anything imports ctranslate2
        logger.info("[Main] add-ons: %s", packs.activate())
    except Exception as e:  # noqa: BLE001 - add-ons are optional; the app runs without them
        logger.warning("[Main] add-ons not activated err=%s: %s", type(e).__name__, e)
    if paths.DATA_DIR_REASON != "default":
        logger.warning("[Main] DATA DIR OVERRIDE ACTIVE: %s", paths.DATA_DIR)

    from core import settings as settings_mod
    from server.app import Ctx, create_app

    cfg = settings_mod.load()
    if (args.verbose or cfg.server.verbose_console) and not tray:
        from utils.logging_config import set_console_verbose
        set_console_verbose(True)
    paths.ensure_dirs()
    if not cfg.audio.track_names:
        # the OBS profile names the tracks (Game / Microphone / Discord ...): roles follow them
        from utils import obs
        names = obs.track_names()
        if names:
            cfg.audio.track_names = names
            settings_mod.save(cfg)
    if not paths.SETTINGS_FILE.exists():
        settings_mod.save(cfg)

    if args.scan:
        ctx = Ctx.build(cfg)
        return _headless_scan(ctx, args.scan)

    if tray:
        from desktop import tray as tray_mod
        ok, why = tray_mod.available()
        if not ok:
            logger.warning("[Main] --tray without pystray (%s) -- serving in the foreground instead", why)
            tray = False

    host = cfg.server.host
    running, port = find_instance_or_port(host, configured_port(cfg))
    browse_host = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    if running is not None:
        logger.info("[Main] instance already serving on %s:%d -- opening browser and exiting", host, running)
        if not args.no_browser and cfg.server.open_browser:
            webbrowser.open(f"http://{browse_host}:{running}/")
        return 0
    base = configured_port(cfg)
    sock, port = (None, None) if port is None else claim_port(host, port, max(0, base + PORT_TRIES - port))
    if sock is None:
        logger.error("[Main] no free port in %d..%d -- not starting", base, base + PORT_TRIES)
        return 1

    ctx = Ctx.build(cfg)
    ctx.run_mode, ctx.port = ("tray" if tray else "console"), port
    app = create_app(ctx)
    runner = Runner(ctx, app, host, port, tray, sock)
    url = f"http://{browse_host}:{port}/"
    # every way out is wired BEFORE the server answers: a quit that arrives the moment /api/status does (the
    # installer, a fast script) must find its hook, and a shutdown that finishes before icon.run() still stops it
    icon = None
    if tray:
        from desktop import tray as tray_mod
        icon = tray_mod.Tray(url=lambda: url, on_quit=lambda: runner.shutdown("tray quit"), work=ctx)
        runner.on_stopped = icon.stop
    if not tray and not no_console:
        runner.install_console_close_handler()      # closing the window = a (time-boxed) clean stop
    ctx.request_quit = lambda: threading.Thread(target=runner.shutdown, args=("http quit",), name="quit",
                                                daemon=True).start()
    ctx.start()
    if not runner.start():
        logger.error("[Main] server did not start on port %d err=%s", port, runner.error)
        runner.shutdown("start failed")
        return 1
    _banner(ctx, host, port, tray=tray)
    if icon is not None:
        ctx.updater.listeners.append(icon.set_update)  # "Update available..." in the tray menu + a notification
    ctx.updater.start()                                 # automatic update check (server runs only, never --scan)
    if not args.no_browser and cfg.server.open_browser:
        from core.workmode import normal_priority
        with normal_priority():                     # a browser we start never inherits the work mode's low priority
            webbrowser.open(url)

    if icon is not None:
        def _watch():                    # the server dying on its own must take the icon with it
            runner.thread.join()
            runner.shutdown("server ended")
        threading.Thread(target=_watch, name="server-watch", daemon=True).start()
        try:
            icon.run()
        except Exception:  # noqa: BLE001 - a broken tray must not leave a headless server nobody can stop
            logger.exception("[Main] tray icon failed -- stopping")
        runner.shutdown("tray ended")
    else:
        runner.wait_console()
    logger.info("[Main] server stopped -- exiting")
    return 0


def _server_settings() -> tuple[str, int, bool]:
    """(host, port, open_browser) straight from settings.json -- read-only: a guard / a losing launch must not
    migrate or rewrite the file the running instance owns."""
    from utils import paths
    host, port, browser = "127.0.0.1", 8558, True
    try:
        srv = json.loads(paths.SETTINGS_FILE.read_text(encoding="utf-8")).get("server") or {}
        host = str(srv.get("host") or host)
        port = int(srv.get("port") or port)
        browser = bool(srv.get("open_browser", True))
    except (OSError, ValueError, TypeError, AttributeError):
        pass

    class _Cfg:                                   # configured_port() only reads cfg.server.port
        class server:
            pass
    _Cfg.server.port = port
    return host, configured_port(_Cfg), browser


def _check_running() -> int:
    """The reopen guard for start scripts, by the same rules as a launch (instance.json, the data dir in /api/status, the lock)."""
    host, port, _b = _server_settings()
    running, _free = find_instance_or_port(host, port, tries=0)
    if running is None and lock_holder() is not None:
        holder = lock_holder() or {}
        if holder.get("mode") == "scan":
            print("scan")                       # callers read stdout only
            return 2
        running = wait_for_winner(host)
    if running is None:
        return 1
    print(running)
    return 0


def _lost_lock(args) -> int | None:
    """Another process holds this data dir: open the browser at its port once it answers, never start a second
    server on another port (contract: wait up to LOCK_WAIT_S, then exit 0 / 1). None = the holder went away and
    this launch now holds the lock itself (the caller starts normally)."""
    from utils import paths
    holder = lock_holder() or {}
    if args.scan:
        logger.error("[Main] the data dir is in use by another WardogClips process pid=%s mode=%s -- stop it first"
                     " (a headless scan next to the app would run two job queues on one database)",
                     holder.get("pid"), holder.get("mode"))
        return 2
    if holder.get("mode") == "scan":
        logger.error("[Main] a headless scan is using this data dir pid=%s -- wait for it to finish or stop it"
                     " (Ctrl+C in its window)", holder.get("pid"))
        return 2
    host, _port, want_browser = _server_settings()
    logger.info("[Main] another launch holds the data dir pid=%s -- waiting up to %d s for it to answer",
                holder.get("pid"), int(LOCK_WAIT_S))
    t0 = time.monotonic()
    port = None
    while port is None and time.monotonic() - t0 < LOCK_WAIT_S:
        port = wait_for_winner(host, timeout=max(0.0, LOCK_WAIT_S - (time.monotonic() - t0)))
        if port is None and lock_holder() is None and acquire_instance_lock("tray" if args.tray else "console"):
            logger.info("[Main] the other launch went away -- starting here")
            return None
    if port is None:
        logger.error("[Main] the WardogClips holding %s did not answer within %d s -- not starting a second one",
                     paths.DATA_DIR, int(LOCK_WAIT_S))
        return 1
    browse_host = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    logger.info("[Main] instance already serving on %s:%d -- opening browser and exiting", host, port)
    if not args.no_browser and want_browser:
        webbrowser.open(f"http://{browse_host}:{port}/")
    return 0


def _headless_scan(ctx, mode: str) -> int:
    from utils.logging_config import rule
    summary = ctx.library.scan()
    if mode == "all":
        for row in ctx.db.query("SELECT id FROM recordings WHERE missing=0"):
            ctx.db.execute("UPDATE recordings SET status='new' WHERE id=? AND status='error'", (row["id"],))
            ctx.jobs.enqueue(row["id"], "probe")
    ctx.jobs.recover()
    n = ctx.jobs.run_pending(lane="io")
    n_stt = n_det = 0
    from core.library import _priority, parse_name
    lanes = []
    if mode in ("transcribe", "transcribe-all", "full") and not ctx.cfg.stt.enabled:
        logger.warning("[Scan] speech-to-text is off (Settings > Speech-to-text) -- skipping transcribe")
    elif mode in ("transcribe", "transcribe-all", "full"):
        # transcribe / full: never transcribed + made before the mic pass; transcribe-all: every recording
        from core import transcripts
        scope = "all" if mode == "transcribe-all" else "outdated"
        rows = transcripts.candidates(ctx.db, scope)
        for rec in rows:
            ctx.jobs.enqueue(rec["id"], "transcribe", priority=_priority(parse_name(rec["name"])[1], rec["mtime"]))
        logger.info("[Scan] transcribing %d recordings scope=%s", len(rows), scope)
        lanes.append("stt")
    if mode in ("detect", "full"):
        rows = ctx.db.query("SELECT * FROM recordings WHERE status='probed' AND missing=0"
                            " AND json_extract(stages, '$.detect.state') IS NULL")
        for rec in rows:
            ctx.jobs.enqueue(rec["id"], "detect", priority=_priority(parse_name(rec["name"])[1], rec["mtime"]))
        logger.info("[Scan] detecting on %d recordings", len(rows))
        lanes.append("video")
    if lanes:
        # drive-aware parallelism: benchmark drives never measured (they are idle right now), then
        # work both lanes with the threaded workers -- one slot per drive and stage, GPU capped
        j = ctx.cfg.jobs
        if j.auto_bench and str(j.parallel).lower() == "auto":
            ctx.jobs.drives.bench_missing()
        for d in ctx.jobs.drive_status():
            logger.info("[Scan] drive=%s kind=%s slots detect=%d transcribe=%d queued=%d", d["drive"], d["kind"] or "unbenchmarked",
                        d["slots"]["detect"], d["slots"]["transcribe"], d["queued"])
        logger.info("[Scan] working lanes=%s gpu_parallel=%d (Ctrl+C once: finish the running jobs; twice: stop them)",
                    ",".join(lanes), j.gpu_parallel)
        res = ctx.jobs.drain(lanes=tuple(lanes))
        n_stt = sum(res["by_stage"].get("transcribe", {}).values())
        n_det = sum(res["by_stage"].get("detect", {}).values())
    s = ctx.library.summary()["total"]
    logger.info(rule())
    logger.info("[Scan] found=%d new=%d moved=%d missing=%d growing=%d | probes run=%d transcribed=%d detected=%d | library files=%d hours=%.1f",
                summary["found"], summary["new"], summary["moved"], summary["missing"], summary["growing"], n, n_stt, n_det,
                s["files"], s["secs"] / 3600.0)
    logger.info(rule())
    return 0


def _banner(ctx, host: str, port: int, tray: bool = False) -> None:
    from media import ffmpeg
    from utils import paths
    from utils.logging_config import console_level, rule
    log = logger
    hw = ffmpeg.hw_info()
    s = ctx.library.summary()
    log.info(rule())
    log.info("WardogClips v%s -- WARDOGS footage scanner for Resolve", paths.APP_VERSION)
    log.info(rule())
    for root in ctx.cfg.library.roots:
        from pathlib import Path
        log.info("  Library %-32s %s", root, "ok" if Path(root).is_dir() else "NOT REACHABLE")
    log.info("Indexed: %d files, %.1f h, %.0f GB (%d probed, %d missing)", s["total"]["files"], s["total"]["secs"] / 3600.0,
             s["total"]["bytes"] / 1e9, s["total"]["probed"], s["total"]["missing"])
    log.info("ffmpeg: %s | cuda=%s hevc_cuvid=%s h264_nvenc=%s", hw.get("version") or "MISSING", hw["cuda"],
             hw["hevc_cuvid"], hw["h264_nvenc"])
    try:
        from utils import gpus
        use = {a["key"] for a in gpus.usable(ctx.cfg)}
        log.info("Graphics: %s", "; ".join(f"{gpus.label(a)} ({'in use' if a['key'] in use else 'left alone'})"
                                           for a in gpus.hardware()) or "none found -- video work on the CPU")
    except Exception:  # noqa: BLE001 - the banner never stops a start
        log.warning("Graphics: the card list could not be read", exc_info=True)
    log.info("Player: %s | squad: %s", ", ".join(ctx.cfg.player.aliases) or "-", ", ".join(ctx.cfg.player.squad) or "-")
    log.info("Jobs: %s", ctx.jobs.counts())
    log.info("UI: http://%s:%d/", host, port)
    log.info("Log: %s (console %s)", paths.LOG_FILE, console_level())
    log.info(rule())
    if tray:
        log.info("Startup complete -- running in the tray (right-click the WardogClips icon > Quit)")
    else:
        log.info("Startup complete -- CLOSE THIS WINDOW (or Ctrl+C) TO STOP")
    log.info(rule())


if __name__ == "__main__":
    sys.exit(main())
