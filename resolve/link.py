"""Keeping DaVinci Resolve ready for WardogClips: start it when it is not running, wait until it
answers and a project is open, and run the hand-offs that were asked for in the meantime.

- At WardogClips start (``startup``, Settings > Resolve > launch_on_start): Resolve is started if it
  does not answer, and the link waits for a project (or opens ``resolve.project`` itself).
- A hand-off (markers / selects) while Resolve is down or has no project (``submit``,
  launch_on_send): Resolve is started if needed, the hand-off waits, and runs as soon as a project
  is open. The page sees every step on the SSE ``resolve`` event and can cancel a waiting hand-off.

Detection never enumerates processes (ban safety: the game process is never enumerated): "is it up"
is the scripting probe (resolve/client.py, a child interpreter), and "is it starting right now" is
Resolve's own log file being written in the last minute (a stat, nothing opened). A freshly started
Resolve shows the Project Manager over an "Untitled Project": that counts as "no project yet"
(``accept_untitled`` = use it anyway, from the page).
"""

import itertools
import logging
import os
import subprocess
import sys
import threading
import time

from resolve import client as rc

logger = logging.getLogger("wclips.resolve")

DEFAULT_EXE = r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe"
UNTITLED = ("", "untitled project")
POLL_S = 2.0                 # between probes while waiting (a "down" probe itself takes ~4 s)
LOG_ACTIVE_S = 60.0          # Resolve's log written this recently = it is starting / running: do not launch again
STATES = ("off", "idle", "down", "starting", "no_project", "ready", "failed")   # idle = nothing watching


def resolve_exe(cfg) -> str:
    return (cfg.resolve.exe or "").strip() or DEFAULT_EXE


def log_file() -> str:
    appdata = os.environ.get("APPDATA") or ""
    return os.path.join(appdata, "Blackmagic Design", "DaVinci Resolve", "Support", "logs", "davinci_resolve.log")


def recently_active(seconds: float = LOG_ACTIVE_S) -> bool:
    """Resolve writes its log continuously while it starts up: a fresh mtime means 'already coming up'."""
    try:
        return time.time() - os.stat(log_file()).st_mtime < seconds
    except OSError:
        return False


def _explorer() -> str | None:
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    p = os.path.join(windir, "explorer.exe")
    return p if os.path.isfile(p) else None


def launch(exe: str) -> None:
    """Start Resolve as a double-click does: through explorer.exe, so it is Explorer's child with Explorer's environment
    (not this app's PATH with the venv's CUDA DLL dirs, RESOLVE_SCRIPT_LIB, our process group). Probes are held until
    its script server is up (client.hold_probes): probing a Resolve that is still starting can make its script server
    fail. Falls back to a detached Popen where there is no explorer.exe."""
    if not os.path.isfile(exe):
        raise rc.ResolveUnavailable(f"Resolve.exe not found at {exe} (Settings > Resolve > exe)")
    exe = os.path.normpath(os.path.abspath(exe))     # explorer.exe opens a folder for a path with forward slashes
    # a session log on disk = a Resolve is already running (busy, or its script server is dead): no fresh start to
    # protect, and a hold would only hide that Resolve for STARTUP_HOLD_S
    hold = not rc.session_log_present()
    if hold:
        rc.hold_probes(time.time())
    try:
        ex = _explorer() if sys.platform == "win32" and "," not in exe else None
        if ex:
            # explorer.exe hands the start to the running shell and exits at once (exit code 1 even on success)
            subprocess.Popen([ex, exe], close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            logger.info("[Resolve] started via explorer exe=%s", exe)
            return
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([exe], cwd=os.path.dirname(exe), creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info("[Resolve] started exe=%s", exe)
    except BaseException:
        if hold:
            rc.release_probes()
        raise


def has_project(name) -> bool:
    return bool(name) and str(name).strip().lower() not in UNTITLED


class ResolveLink:
    """One per app (ctx.resolve_link). A watcher thread runs only while something waits on Resolve."""

    def __init__(self, cfg, bus, probe=None, launcher=None, loader=None, active=None):
        self.cfg, self.bus = cfg, bus
        self._probe = probe or (lambda: rc.probe_cached(max_age=0.0))
        self._launcher = launcher              # None = the module's launch(), looked up at call time
        self._load = loader or self._load_project
        self._active = active or recently_active
        self._lock = threading.RLock()
        self._launch_ok = False
        self.assumed_start = False             # Resolve's fresh log said "starting": we did not launch it
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ids = itertools.count(1)
        self.ops: list[dict] = []              # waiting / running hand-offs
        self.done: list[dict] = []             # the last few finished ones (for the page after a reload)
        self.state, self.project, self.error = "down", None, None
        self.since = time.time()
        self.launched_at: float | None = None  # monotonic; this waiting cycle started Resolve (or saw it starting)
        self._launch_wall = 0.0                  # the same moment in wall time (compared with Resolve's log mtime)
        self.launched_once = False             # we called launch() in this watcher's life: never a second copy
        self.watch_until = 0.0                 # monotonic; startup keeps watching until a project is open (or this)
        self.accepted_project: str | None = None   # "use the open project": this one name, until Resolve goes / ops ran
        self.tried_project: str | None = None  # resolve.project LoadProject'ed (or definitely missing) this session
        self._load_tries = 0
        self._cancelled = False                # Cancel all ended the wait (not a failed launch)
        self.last_ready: tuple | None = None   # (project, utc) the last time Resolve was ready (state may be idle since)
        self._down_streak = 0                  # consecutive "not connected" probes

    def accepts(self, name) -> bool:
        """A project hand-offs may go to: a real one, or the one the page said to use (also an Untitled Project)."""
        return has_project(name) or (bool(name) and name == self.accepted_project)

    # ---- public ---------------------------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {"state": self.state, "project": self.project, "error": self.error, "since": self.since,
                    "last_ready": list(self.last_ready) if self.last_ready else None,
                    "launched": self.launched_at is not None, "watching": bool(self._thread and self._thread.is_alive()),
                    "waiting": [self._op_view(o) for o in self.ops],
                    "recent": [self._op_view(o) for o in self.done[-5:]],
                    "launch_on_start": bool(self.cfg.resolve.launch_on_start),
                    "launch_on_send": bool(self.cfg.resolve.launch_on_send),
                    "exe": resolve_exe(self.cfg), "auto_project": self.cfg.resolve.project or ""}

    def startup(self) -> None:
        """WardogClips start: get Resolve up with a project (Settings > Resolve > launch_on_start)."""
        if not self.cfg.resolve.enabled or not self.cfg.resolve.launch_on_start:
            return
        self.watch_until = time.monotonic() + 60.0 * self.cfg.resolve.wait_min
        self._ensure_thread(launch_ok=True)

    def start_now(self) -> dict:
        """The page asked: start Resolve (if needed) and wait for a project."""
        if not self.cfg.resolve.enabled:
            raise rc.ResolveError("the Resolve hand-off is disabled in Settings > Resolve")
        self.watch_until = time.monotonic() + 60.0 * self.cfg.resolve.wait_min
        with self._lock:
            if self.state in ("failed", "idle", "down"):
                # a fresh attempt; while 'starting' the running attempt keeps its launch (no second copy)
                if self.state == "failed":
                    self._set("down", None)
                self.launched_at, self.assumed_start, self.launched_once = None, False, False
        self._ensure_thread(launch_ok=True)
        return self.snapshot()

    def submit(self, kind: str, label: str, fn) -> dict:
        """Queue a hand-off to run once Resolve has a project; returns the op as the page sees it."""
        op = {"id": next(self._ids), "kind": kind, "label": label, "fn": fn, "state": "waiting",
              "created": time.time(), "deadline": time.monotonic() + 60.0 * self.cfg.resolve.wait_min,
              "result": None, "error": None}
        with self._lock:
            self.ops.append(op)
            if self.state == "failed":
                self._set("down", None)
        logger.info("[Resolve] hand-off waiting op=%d kind=%s %s", op["id"], kind, label)
        self._publish("queued", op=self._op_view(op))
        self._ensure_thread(launch_ok=bool(self.cfg.resolve.launch_on_send))
        return self._op_view(op)

    def cancel(self, op_id: int | None = None) -> int:
        """Drop waiting hand-offs (one, or all); a running one finishes."""
        with self._lock:
            gone = [o for o in self.ops if o["state"] == "waiting" and (op_id is None or o["id"] == op_id)]
            for o in gone:
                o["state"], o["error"] = "cancelled", "cancelled"
                self.ops.remove(o)
                self.done.append(o)
            if op_id is None:
                self.watch_until = 0.0
                self._cancelled = True                 # the settle must not call this a failed launch
        for o in gone:
            self._publish("cancelled", op=self._op_view(o))
        self._wake.set()
        return len(gone)

    def use_open_project(self, name: str | None = None) -> dict:
        """The page: the project open right now is the one (also an 'Untitled Project'). One-shot: it
        lasts until the waiting hand-offs ran or Resolve goes away."""
        with self._lock:
            self.accepted_project = name or self.project
            self.watch_until = max(self.watch_until, time.monotonic() + 30.0)   # get to 'ready' even with nothing queued
        self._ensure_thread(launch_ok=False)
        return self.snapshot()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None:
            t.join(10)

    # ---- the watcher ----------------------------------------------------------------------------
    def _ensure_thread(self, launch_ok: bool) -> None:
        with self._lock:
            self._launch_ok = self._launch_ok or launch_ok
            if self._thread is not None:               # alive: cleared (under this lock) when it ends
                self._wake.set()
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="resolve-link", daemon=True)
            self._thread.start()

    def _exit_if_idle(self) -> bool:
        """Nothing waits on Resolve: the watcher ends -- decided under the lock, so a submit() at the same
        moment either sees this thread gone (and starts a new one) or is seen here."""
        with self._lock:
            if self.ops or time.monotonic() < self.watch_until:
                return False
            if self._thread is threading.current_thread():
                self._thread, self._launch_ok = None, False
            # settle: nothing watches any more, so no state may claim to be in progress. A launch that
            # never answered within the window is a failure (with the scripting hint); anything else
            # becomes 'idle' and the page goes by the plain status probe
            started, cancelled = self.launched_at, self._cancelled
            self.launched_at, self.assumed_start, self.launched_once, self._cancelled = None, False, False, False
            if cancelled or started is None:
                rc.release_probes()                    # no launch of ours is still coming up
            self._down_streak = 0
            if self.state == "starting" and started is not None and not cancelled:
                # the watch window ran out while Resolve was still not answering
                self._set("failed", None, f"Resolve did not answer within {self.cfg.resolve.wait_min} min -- "
                                          + rc.SCRIPTING_HINT)
            elif self.state in ("starting", "no_project", "down", "ready"):
                self._set("idle", None)            # a later Resolve session starts from scratch
            # accepted_project stays: it ends when Resolve is seen gone (seen_status / _down) or after it was used
            return True

    def seen_status(self, st: dict) -> None:
        """A status probe from anywhere (the page polls every 15 s): Resolve not answering ends its session,
        so a "use the open project" or a project auto-open never carries over to the next Resolve."""
        if st and st.get("available") and not st.get("connected"):
            with self._lock:
                self.accepted_project = None
                self.tried_project, self._load_tries = None, 0

    def _run(self) -> None:
        logger.debug("[Resolve] link watcher started")
        try:
            while not self._stop.is_set():
                if not self.cfg.resolve.enabled:       # switched off in Settings while something waited
                    self._fail("the Resolve hand-off is disabled in Settings > Resolve")
                    self._set("off", None)
                    with self._lock:
                        if self._thread is threading.current_thread():
                            self._thread, self._launch_ok = None, False
                    break
                self._expire()
                if self._exit_if_idle():
                    break
                st = self._probe() or {}
                if st.get("connected"):
                    with self._lock:
                        # Resolve answered: whatever launch attempt was running is over (a later "down" is a
                        # new attempt that may launch again), and a single failed probe is forgotten
                        self.launched_at, self.assumed_start, self.launched_once = None, False, False
                        self._down_streak = 0
                if not st.get("connected"):
                    self._down(st.get("error"))
                elif not self.accepts(st.get("project")):
                    self._no_project(st.get("project"))
                else:
                    self._set("ready", st.get("project"))
                    ran = self._run_ops()
                    if ran is not None:
                        with self._lock:
                            self.watch_until = 0.0     # startup's job is done; hand-offs keep it alive
                            if ran and not self.ops:
                                self.accepted_project = None   # "use the open project" covered these hand-offs
                        if self._exit_if_idle():
                            break
                self._wake.wait(POLL_S)
                self._wake.clear()
        except Exception:                              # noqa: BLE001 -- the watcher must never take the app down
            logger.exception("[Resolve] link watcher crashed")
            self._fail("internal error in the Resolve link (see the log)")
        finally:
            with self._lock:
                if self._thread is threading.current_thread():     # crashed / stopped: still registered
                    self._thread, self._launch_ok = None, False
            logger.debug("[Resolve] link watcher stopped state=%s", self.state)

    def _down(self, err) -> None:
        with self._lock:
            self._down_streak += 1
            if self.state in ("ready", "no_project") and self._down_streak < 2:
                return                                 # one failed probe of a Resolve that was up: busy, not gone
            # Resolve is gone: a "use this project" and a project auto-open belong to the session that ended
            self.accepted_project = None
            self.tried_project, self._load_tries = None, 0
            launched = self.launched_at
            launch_ok = self._launch_ok and not self.launched_once      # never a second copy per attempt
            if launched is not None and self.assumed_start and not self._active():
                # we only ASSUMED it was starting (fresh log) and the log went quiet without Resolve ever
                # answering: it was closing, not opening -> start it after all (unless we already did)
                self.launched_at, self.assumed_start, launched = None, False, None
                if self.launched_once:
                    launched = time.monotonic() - 60.0 * self.cfg.resolve.wait_min - 1   # -> the timeout below
        if launched is None and launch_ok and err == rc.SCRIPT_SERVER_DEAD:
            # Resolve is open but its scripting server gave up: a second Resolve.exe cannot help, only a restart
            self._fail(err)
            return
        if launched is None and launch_ok:
            if self._active():
                # Resolve wrote its log within the last minute: it is starting (a manual launch, or
                # ours from a previous WardogClips run). Starting a second copy would only show an error.
                with self._lock:
                    self.launched_at, self.assumed_start = time.monotonic(), True
                    self._launch_wall = time.time()
                self._set("starting", None, "Resolve is starting...")
                return
            try:
                (self._launcher or launch)(resolve_exe(self.cfg))
            except Exception as e:                     # noqa: BLE001 -- shown on the page
                logger.warning("[Resolve] could not start Resolve: %s", e)
                self._fail(f"could not start Resolve: {e}")
                return
            with self._lock:
                self.launched_at, self.assumed_start, self.launched_once = time.monotonic(), False, True
                self._launch_wall = time.time()
            self._set("starting", None, "Resolve is starting...")
            return
        if launched is not None:
            if err == rc.SCRIPT_SERVER_DEAD and self._launch_wall and self._debug_log_since(self._launch_wall):
                # this Resolve session gave up on its scripting server: waiting the full wait_min cannot help
                self._fail(err)
                return
            if time.monotonic() - launched > 60.0 * self.cfg.resolve.wait_min:
                self._fail(f"Resolve did not answer within {self.cfg.resolve.wait_min} min -- " + rc.SCRIPTING_HINT)
            else:
                self._set("starting", None, "Resolve is starting...")
            return
        self._set("down", None, err or rc.NOT_RUNNING)

    @staticmethod
    def _debug_log_since(t_wall: float) -> bool:
        """Was Resolve's debug log written after t_wall (= it belongs to the session we are waiting for)?"""
        try:
            return os.stat(rc.debug_log_file()).st_mtime >= t_wall - 5.0
        except OSError:
            return False

    def _no_project(self, name) -> None:
        want = (self.cfg.resolve.project or "").strip()
        if want and self.tried_project != want and self._load_tries < 3:
            self._load_tries += 1
            self._set("no_project", name, f"opening project '{want}'...")
            try:
                if self._load(want):
                    self.tried_project = want
                    logger.info("[Resolve] opened project %s", want)
                    rc.invalidate_probe()
                    return
                self.tried_project = want             # definitely not there: ask once per Resolve session
                msg = f"Resolve has no project named '{want}' -- open one yourself (or fix Settings > Resolve > project)"
            except Exception as e:                     # noqa: BLE001 -- transient (busy / pending): up to 3 tries
                msg = f"could not open project '{want}': {e}"
            logger.warning("[Resolve] %s", msg)
            self._set("no_project", name, msg)
            return
        if self.state != "no_project" or self.error is None:
            self._set("no_project", name, "open a project in Resolve (the Project Manager)")

    def _load_project(self, name: str) -> bool:
        r = rc.connect()
        pm = rc._call(r, "GetProjectManager")
        if pm is None:
            raise rc.ResolveError("no project manager (API refused)")
        return bool(rc._in_thread(lambda: pm.LoadProject(name), 60.0, "load project"))

    def _run_ops(self) -> int | None:
        """Run the waiting hand-offs in order. Returns how many ran once the queue is drained (0 = there
        was nothing), None when Resolve stopped answering half way (the rest wait for the next poll)."""
        ran = 0
        while not self._stop.is_set():
            with self._lock:
                op = next((o for o in self.ops if o["state"] == "waiting"), None)
                if op is None:
                    return ran
                op["state"] = "running"
            self._publish("running", op=self._op_view(op))
            try:
                op["result"] = op["fn"]()
                op["state"] = "done"
                logger.info("[Resolve] hand-off done op=%d kind=%s", op["id"], op["kind"])
            except rc.ResolveNotRunning as e:
                # transient (connect still pending / timed out): back in the queue, retried on the next poll
                # within its deadline -- the route queues on exactly this error expecting a retry. The reason
                # stays on the op, so the page (and a timeout) says why it is still waiting
                with self._lock:
                    op["state"], op["note"] = "waiting", f"Resolve did not answer the hand-off: {e}"
                rc.invalidate_probe()
                logger.info("[Resolve] hand-off op=%d waits again: %s", op["id"], e)
                self._publish("state")
                return None
            except Exception as e:                     # noqa: BLE001 -- reported to the page
                op["state"], op["error"] = "failed", str(e)
                logger.warning("[Resolve] hand-off failed op=%d kind=%s error=%s", op["id"], op["kind"], e)
            with self._lock:
                if op in self.ops:
                    self.ops.remove(op)
                self.done.append(op)
                self.done = self.done[-20:]
            self._publish("done" if op["state"] == "done" else "failed", op=self._op_view(op, result=True))
            ran += 1
        return None

    def _expire(self) -> None:
        now = time.monotonic()
        with self._lock:
            gone = [o for o in self.ops if o["state"] == "waiting" and now > o["deadline"]]
            for o in gone:
                o["state"] = "failed"
                o["error"] = (f"gave up after {self.cfg.resolve.wait_min} min waiting for Resolve "
                              f"({o.get('note') or (self.state.replace('_', ' ') + ': ' + (self.error or ''))})")
                self.ops.remove(o)
                self.done.append(o)
        for o in gone:
            logger.warning("[Resolve] hand-off timed out op=%d kind=%s", o["id"], o["kind"])
            self._publish("failed", op=self._op_view(o))

    def _fail(self, msg: str) -> None:
        with self._lock:
            waiting = [o for o in self.ops if o["state"] == "waiting"]
            for o in waiting:
                o["state"], o["error"] = "failed", msg
                self.ops.remove(o)
                self.done.append(o)
            self.watch_until = 0.0
            self.launched_at, self.assumed_start, self.launched_once = None, False, False
        rc.release_probes()                            # nothing waits on that launch any more
        self._set("failed", None, msg)
        for o in waiting:
            self._publish("failed", op=self._op_view(o))

    # ---- state + events -------------------------------------------------------------------------
    def _set(self, state: str, project, error=None) -> None:
        with self._lock:
            changed = self._apply(state, project, error)
        if changed:
            logger.info("[Resolve] link state=%s project=%s %s", state, project, error or "")
            self._publish("state")

    def _apply(self, state, project, error) -> bool:
        if (state, project, error) == (self.state, self.project, self.error):
            return False
        if state != self.state:
            self.since = time.time()
        if state == "ready":
            self.launched_at, self.assumed_start, self.launched_once = None, False, False
            self.last_ready = (project, time.time())
            error = None
        self.state, self.project, self.error = state, project, error
        return True

    @staticmethod
    def _op_view(o: dict, result: bool = False) -> dict:
        v = {k: o[k] for k in ("id", "kind", "label", "state", "created", "error")}
        if o.get("note"):
            v["note"] = o["note"]
        if result and isinstance(o.get("result"), dict):
            r = o["result"]
            v["result"] = {k: r.get(k) for k in ("project", "sent", "timeline", "clips", "events", "failed", "imported",
                                                 "note", "bins") if k in r}
        return v

    def _publish(self, action: str, **extra) -> None:
        try:
            self.bus.publish("resolve", action=action, link=self.snapshot(), **extra)
        except Exception:                              # noqa: BLE001
            logger.debug("[Resolve] publish failed", exc_info=True)
