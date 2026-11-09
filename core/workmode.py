"""Work modes: keep the PC usable while WardogClips works.

Settings > Performance > ``performance.mode``:
- ``full``: everything at normal priority, no limits.
- ``balanced`` (default): our process and every ffmpeg / yt-dlp child at below-normal priority; while you play
  (a game, a full-screen app or an OBS recording is noticed) everything drops to idle priority, fewer jobs run at
  once and the CPU thread counts shrink.
- ``background``: idle priority always, and new jobs start only after you have been away from the PC for
  ``performance.idle_min`` minutes and nothing runs full screen.

"Busy" is noticed WITHOUT looking at any process (ban safety: nothing is listed, opened or signalled):
- the 3D load of the busiest graphics card from Windows' GPU Engine counters (utils/gpuload.py: summed values, the
  instance names are never kept), at or above ``performance.gpu_busy_pct``. Skipped while our own OCR is on the GPU
  (its reads would count as "a game").
- the shell's notification state (SHQueryUserNotificationState): a full-screen Direct3D app, a full-screen app
  ("busy") or presentation mode.
- a video encoder session that is not ours (OBS recording / streaming / replay buffer on NVENC; media/encoder.py).
- your input idle time (GetLastInputInfo) -- only for the background mode's "are you away" check.
Busy stays on for HOLD_S after the last signal so a loading screen does not flip the mode back and forth.

Nothing running is stopped or slowed abruptly: priority changes apply live (to our process and the children still
running), limits apply to NEW work (claims, OCR engines handed out, ffmpeg / whisper thread counts at their start).

One monitor per process: ``Ctx`` builds it, ``start()`` makes it the active one (``current()``); without an active
monitor (tests, --scan) every helper here answers as before (no limits, the old x264 below-normal priority).
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
import weakref

from core.messages import MsgError
from utils.i18n import msg

logger = logging.getLogger("wclips.workmode")

MODES = ("full", "balanced", "background")
TICK_S = 5.0                 # how often the signals are read
ENCODER_EVERY_S = 15.0       # nvidia-smi is a child process: the encoder signal is read less often
HOLD_S = 60.0                # busy stays on this long after the last signal

# process priority classes (Windows)
NORMAL = 0x00000020
BELOW_NORMAL = 0x00004000
IDLE = 0x00000040
_RANK = {IDLE: 0, BELOW_NORMAL: 1, NORMAL: 2}
_PRIO_NAME = {IDLE: "idle", BELOW_NORMAL: "below_normal", NORMAL: "normal"}

# SHQueryUserNotificationState
QUNS_NOT_PRESENT = 1
QUNS_BUSY = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4
QUNS_ACCEPTS_NOTIFICATIONS = 5
QUNS_QUIET_TIME = 6
QUNS_APP = 7
_QUNS_NAME = {1: "screen_locked", 2: "fullscreen_app", 3: "fullscreen_d3d", 4: "presentation", 5: "normal",
              6: "quiet_time", 7: "store_app_fullscreen"}

# user-facing text: whole sentences (one per state), English + the catalog keys server.workmode.label.<mode> /
# note.<key> / help.<mode> (the snapshot carries label_i18n / note_i18n / help_i18n for the page)
LABELS = {"full": msg("server.workmode.label.full", "Full speed")["message"],
          "balanced": msg("server.workmode.label.balanced", "Balanced")["message"],
          "background": msg("server.workmode.label.background", "Background")["message"]}
NOTES = {
    "game": msg("server.workmode.note.game", "Slowed down: game running")["message"],
    "fullscreen": msg("server.workmode.note.fullscreen", "Slowed down: full-screen app")["message"],
    "recording": msg("server.workmode.note.recording", "Slowed down: recording")["message"],
    "waiting": msg("server.workmode.note.waiting", "Waiting until you're away")["message"],
    "paused": msg("server.workmode.note.paused", "Paused while you play")["message"],
}
HELP = {
    "full": msg("server.workmode.help.full",
                "Full speed: WardogClips uses all the power it can get, even while you play.")["message"],
    "balanced": msg("server.workmode.help.balanced",
                    "Balanced: WardogClips works at low priority and slows down further while a game, "
                    "a full-screen app or an OBS recording is running.")["message"],
    "background": msg("server.workmode.help.background",
                      "Background: WardogClips starts new work only after you have been away from the PC for a "
                      "while and nothing is running full screen.")["message"],
}


def _i18n(group: str, key: str) -> dict | None:
    return {"code": f"server.workmode.{group}.{key}", "params": {}} if key else None


# ---- signal readers (tests replace the Probes object) -----------------------------------------------------------------
class Probes:
    """The real readers. Each returns None when Windows / the driver cannot say."""

    def gpu3d(self) -> dict | None:
        from utils import gpuload
        return gpuload.sample()

    def notification_state(self) -> int | None:
        if os.name != "nt":
            return None
        try:
            import ctypes
            st = ctypes.c_int(0)
            if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(st)) != 0:
                return None
            return int(st.value)
        except (OSError, AttributeError):
            return None

    def idle_seconds(self) -> float | None:
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
            li = LASTINPUTINFO()
            li.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
                return None
            now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
            return ((now - li.dwTime) & 0xFFFFFFFF) / 1000.0
        except (OSError, AttributeError):
            return None

    def foreign_encoder_sessions(self) -> int | None:
        """Video encoder sessions open on the GPU that are not ours (OBS). None = unknown (no nvidia-smi)."""
        try:
            from media import encoder
            n = encoder.nvenc_sessions(max_age=ENCODER_EVERY_S / 2)
            if n is None:
                return None
            return max(0, int(n) - int(encoder._ours))
        except Exception:  # noqa: BLE001
            return None

    def own_gpu_reads(self) -> int:
        """A counter that grows while our own OCR reads on the GPU (its 3D load is ours, not a game's)."""
        try:
            from vision.ocr import ocr, ocr_fast
            return int(ocr.gpu_calls) + int(ocr_fast.gpu_calls)
        except Exception:  # noqa: BLE001
            return 0


_k32 = None


def _kernel32():
    """kernel32 with HANDLE-sized argtypes (the default int would cut a 64-bit handle to 32 bits)."""
    global _k32
    if _k32 is None:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.GetCurrentProcess.argtypes = []
        k.SetPriorityClass.restype = wintypes.BOOL
        k.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.GetPriorityClass.restype = wintypes.DWORD
        k.GetPriorityClass.argtypes = [wintypes.HANDLE]
        _k32 = k
    return _k32


def _set_process_priority(cls: int) -> bool:
    if os.name != "nt":
        return False
    try:
        k = _kernel32()
        return bool(k.SetPriorityClass(k.GetCurrentProcess(), cls))
    except (OSError, AttributeError):
        return False


def process_priority() -> int | None:
    """Our process's priority class now (for the log / checks), None off Windows."""
    if os.name != "nt":
        return None
    try:
        k = _kernel32()
        return int(k.GetPriorityClass(k.GetCurrentProcess())) or None
    except (OSError, AttributeError):
        return None


def _set_child_priority(proc, cls: int) -> bool:
    """Our OWN child, through the handle Popen holds (never looked up by pid)."""
    if os.name != "nt":
        return False
    h = getattr(proc, "_handle", None)
    if h is None:
        return False
    try:
        return bool(_kernel32().SetPriorityClass(int(h), cls))
    except (OSError, AttributeError, TypeError, ValueError):
        return False


# ---- the monitor ---------------------------------------------------------------------------------------------------
class WorkMode:
    def __init__(self, cfg, bus=None, probes: Probes | None = None):
        self.cfg = cfg
        self.bus = bus
        self.probes = probes or Probes()
        self._lock = threading.Lock()
        self._prio_lock = threading.RLock()     # our process priority class: _apply and normal_priority()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._busy_until = 0.0
        self._last_enc = -1e9
        self._enc_sessions: int | None = None
        self._own_reads = None
        self._applied_prio: int | None = None
        self._state: dict = {}
        self._children: "weakref.WeakSet" = weakref.WeakSet()
        self.listeners: list = []               # callables(state) on a change (the job queue: wake, OCR pool size)
        self._set_proc = _set_process_priority
        self._set_child = _set_child_priority
        self._clock = time.monotonic

    # -- settings ------------------------------------------------------------------------------------------------
    @property
    def perf(self):
        return self.cfg.performance

    @property
    def mode(self) -> str:
        m = str(getattr(self.perf, "mode", "balanced") or "balanced")
        return m if m in MODES else "balanced"

    def set_mode(self, mode: str) -> dict:
        if mode not in MODES:
            raise MsgError(msg("server.workmode.bad_mode", "Unknown work mode: {mode}", mode=str(mode)))
        self.perf.mode = mode
        logger.info("[WorkMode] mode=%s (set by you)", mode)
        return self.tick(force_publish=True, read=False)

    # -- signals -> state ----------------------------------------------------------------------------------------
    def _read(self) -> dict:
        p = self.probes
        now = self._clock()
        gpu = None
        try:
            gpu = p.gpu3d()
        except Exception:  # noqa: BLE001 - a reader failing is "unknown"
            gpu = None
        try:
            quns = p.notification_state()
        except Exception:  # noqa: BLE001
            quns = None
        try:
            idle = p.idle_seconds()
        except Exception:  # noqa: BLE001
            idle = None
        if now - self._last_enc >= ENCODER_EVERY_S:
            self._last_enc = now
            try:
                self._enc_sessions = p.foreign_encoder_sessions()
            except Exception:  # noqa: BLE001
                self._enc_sessions = None
        try:
            reads = p.own_gpu_reads()
        except Exception:  # noqa: BLE001
            reads = 0
        own_gpu = self._own_reads is not None and reads > self._own_reads
        self._own_reads = reads
        return {"gpu": gpu, "quns": quns, "idle_s": idle, "enc": self._enc_sessions, "own_gpu": own_gpu}

    def _compute(self, sig: dict) -> dict:
        perf = self.perf
        mode = self.mode
        now = self._clock()
        gpu = sig.get("gpu") or None
        peak = None if gpu is None else float(gpu.get("peak") or 0.0)
        quns = sig.get("quns")
        reasons = []
        if quns == QUNS_RUNNING_D3D_FULL_SCREEN:
            reasons.append("game")
        elif quns in (QUNS_BUSY, QUNS_PRESENTATION_MODE, QUNS_APP):
            reasons.append("fullscreen")
        if peak is not None and peak >= int(perf.gpu_busy_pct) and not sig.get("own_gpu") and "game" not in reasons:
            reasons.append("game")
        if sig.get("enc"):
            reasons.append("recording")
        reasons.sort(key=("game", "recording", "fullscreen").index)     # the most telling one first
        auto = bool(perf.auto_detect)
        if auto and reasons:
            self._busy_until = now + HOLD_S
        busy = auto and (bool(reasons) or now < self._busy_until)
        reason = reasons[0] if reasons else (self._state.get("reason") if busy else "")
        idle_s = sig.get("idle_s")
        fullscreen = quns in (QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_BUSY, QUNS_PRESENTATION_MODE, QUNS_APP)
        away = (idle_s is None or idle_s >= int(perf.idle_min) * 60) and not fullscreen
        throttled = mode != "full" and busy
        waiting = mode == "background" and not away
        if mode == "full":
            prio = NORMAL
        elif mode == "background" or busy:
            prio = IDLE
        else:
            prio = BELOW_NORMAL
        paused = bool(getattr(self.cfg.jobs, "paused", False))
        if paused:
            note_key = "paused"
        elif waiting:
            note_key = "waiting"
        elif throttled:
            note_key = reason or "game"
        else:
            note_key = ""
        cpus = os.cpu_count() or 4
        caps = {"video_jobs": 1 if throttled else None, "stt_jobs": 1 if throttled else None,
                "ocr_engines": 1 if throttled else None,
                "ffmpeg_threads": max(2, cpus // 4) if throttled else None,
                "whisper_cpu_threads": max(2, cpus // 4) if throttled else None}
        return {
            "mode": mode, "label": LABELS[mode], "note": NOTES.get(note_key, ""), "note_key": note_key,
            "help": HELP[mode], "label_i18n": _i18n("label", mode), "help_i18n": _i18n("help", mode),
            "note_i18n": _i18n("note", note_key) if note_key in NOTES else None, "busy": busy, "reason": reason if busy else "", "signals": reasons,
            "throttled": throttled, "waiting": waiting, "away": away, "paused": paused,
            "auto_detect": auto, "idle_min": int(perf.idle_min), "gpu_busy_pct": int(perf.gpu_busy_pct),
            "priority": _PRIO_NAME[prio], "_prio": prio, "caps": caps,
            "idle_s": None if idle_s is None else round(float(idle_s), 1),
            "gpu_3d": None if peak is None else round(peak, 1),
            "gpu_3d_total": None if gpu is None else gpu.get("total"),
            "gpu_adapters": {} if gpu is None else dict(gpu.get("adapters") or {}),
            "gpu_source": "" if gpu is None else gpu.get("source", ""),
            "own_gpu": bool(sig.get("own_gpu")),
            "screen": _QUNS_NAME.get(quns, "unknown") if quns is not None else "unknown",
            "encoder_sessions": sig.get("enc"),
        }

    _KEYS = ("mode", "busy", "reason", "throttled", "waiting", "paused", "priority", "note_key", "auto_detect")

    def tick(self, force_publish: bool = False, read: bool = True) -> dict:
        """Read the signals (read=False: reuse the last ones, or none yet -- a settings change never waits on the
        hardware readers), recompute, apply priorities, publish on a change."""
        with self._lock:
            sig = self._read() if read else (self._state.get("_sig") or {})
            new = self._compute(sig)
            new["_sig"] = sig
            old = self._state
            changed = any(old.get(k) != new.get(k) for k in self._KEYS)
            self._state = new
        if changed or force_publish:
            self._apply(new)
            if changed:
                logger.info("[WorkMode] mode=%s busy=%s reason=%s throttled=%s waiting=%s priority=%s gpu3d=%s screen=%s"
                            " idle_s=%s encoders=%s", new["mode"], new["busy"], new["reason"] or "-", new["throttled"],
                            new["waiting"], new["priority"], new["gpu_3d"], new["screen"], new["idle_s"],
                            new["encoder_sessions"])
            if self.bus is not None:
                self.bus.publish("workmode", state=self.public(new))
            for cb in list(self.listeners):
                try:
                    cb(new)
                except Exception:  # noqa: BLE001
                    logger.warning("[WorkMode] listener failed", exc_info=True)
        return self.public(new)

    @staticmethod
    def public(state: dict) -> dict:
        return {k: v for k, v in state.items() if not k.startswith("_")}

    def snapshot(self) -> dict:
        with self._lock:
            st = self._state
        # recomputed from the last signals: a pause / mode change made elsewhere shows at once
        return self.tick(read=False) if st else self.tick()

    def state(self) -> dict:
        with self._lock:
            return self._state

    # -- effects --------------------------------------------------------------------------------------------------
    def _apply(self, st: dict) -> None:
        if not self.running:
            return
        with self._prio_lock:
            st = self._state or st                   # two ticks racing: the newest state wins
            prio = st["_prio"]
            if prio != self._applied_prio:
                ok = self._set_proc(prio)
                self._applied_prio = prio
                now = process_priority()
                logger.info("[WorkMode] process priority=%s ok=%s now=%s", _PRIO_NAME[prio], ok,
                            _PRIO_NAME.get(now, now))
        for proc, kind in list(self._children_items()):
            if proc.poll() is None:
                self._set_child(proc, self.child_class(kind, st))

    def _children_items(self):
        for proc in list(self._children):
            yield proc, getattr(proc, "_wclips_kind", "work")

    def child_class(self, kind: str = "work", st: dict | None = None, low_priority: bool = False) -> int:
        st = st if st is not None else self._state
        cls = st.get("_prio", BELOW_NORMAL) if st else BELOW_NORMAL
        if kind == "interactive" and _RANK[cls] < _RANK[BELOW_NORMAL]:
            cls = BELOW_NORMAL                       # a clip you asked for in the player never waits at idle
        if low_priority and _RANK[cls] > _RANK[BELOW_NORMAL]:
            cls = BELOW_NORMAL                       # x264 always yields to the game (as before)
        return cls

    def track(self, proc, kind: str = "work") -> None:
        try:
            proc._wclips_kind = kind
            self._children.add(proc)
        except (TypeError, AttributeError):
            pass

    def claim_refusal(self, lane: str, running_in_lane: int) -> str | None:
        """Why a NEW job of `lane` must wait (None = it may start). Running jobs are never touched."""
        if lane == "io":
            return None                               # probes / thumbnails: tiny
        st = self._state
        if not st:
            return None
        if st.get("waiting"):
            return "waiting"
        caps = st.get("caps") or {}
        cap = caps.get("video_jobs") if lane == "video" else caps.get("stt_jobs") if lane == "stt" else None
        if cap is not None and running_in_lane >= cap:
            return "busy"
        return None

    # -- thread ---------------------------------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        global _current
        if self.running:
            return
        self._stop.clear()
        _current = self
        self._thread = threading.Thread(target=self._loop, name="workmode", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        global _current
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None
        if _current is self:
            _current = None

    def _loop(self) -> None:
        first = True
        while not self._stop.is_set():
            try:
                self.tick(force_publish=first)
                first = False
            except Exception:  # noqa: BLE001 - the monitor must never take the app down
                logger.warning("[WorkMode] tick failed", exc_info=True)
            self._stop.wait(TICK_S)


_current: WorkMode | None = None


def current() -> WorkMode | None:
    """The running monitor, or None (tests, --scan): then nothing is limited."""
    wm = _current
    return wm if wm is not None and wm.running else None


# ---- helpers for the rest of the app ---------------------------------------------------------------------------------
def _interactive_thread() -> bool:
    """Job workers are named job-<lane>-...; anything else (a player clip request, a prefetch) is interactive."""
    return not threading.current_thread().name.startswith("job-")


def child_flags(base: int = 0, low_priority: bool = False, kind: str | None = None) -> int:
    """creationflags for an ffmpeg / yt-dlp child: `base` (CREATE_NO_WINDOW) + the priority class of the work mode.
    Without a running monitor: base, plus BELOW_NORMAL when low_priority (the old x264 rule)."""
    if os.name != "nt":
        return base
    wm = current()
    if wm is None:
        return base | (BELOW_NORMAL if low_priority else 0)
    kind = kind or ("interactive" if _interactive_thread() else "work")
    cls = wm.child_class(kind, low_priority=low_priority)
    return base | (0 if cls == NORMAL else cls)


def track(proc, kind: str | None = None):
    """Remember a started child so a mode change re-applies its priority live. Returns proc."""
    wm = current()
    if wm is not None and proc is not None:
        wm.track(proc, kind or ("interactive" if _interactive_thread() else "work"))
    return proc


@contextlib.contextmanager
def normal_priority():
    """Around starting a program FOR the user (the browser, Resolve, a folder or the log): Windows hands a lowered
    priority class (idle / below normal) down to a child started without its own class, so our process is back at
    normal for that moment. Without a running monitor it does nothing."""
    wm = current()
    if wm is None or os.name != "nt":
        yield
        return
    with wm._prio_lock:
        prev = process_priority()
        raised = prev is not None and prev != NORMAL and _set_process_priority(NORMAL)
        try:
            yield
        finally:
            if raised:
                _set_process_priority(prev)


def claim_refusal(lane: str, running_in_lane: int) -> str | None:
    wm = current()
    return None if wm is None else wm.claim_refusal(lane, running_in_lane)


def _cap(name: str):
    wm = current()
    if wm is None:
        return None
    return ((wm.state() or {}).get("caps") or {}).get(name)


def ocr_engines(n: int) -> int:
    """OCR engines per pool: n normally, fewer while throttled."""
    c = _cap("ocr_engines")
    return max(1, min(int(n), int(c))) if c else max(1, int(n))


def ffmpeg_thread_args() -> list[str]:
    """['-threads', N] for a CPU encode started while throttled, else []."""
    c = _cap("ffmpeg_threads")
    return ["-threads", str(int(c))] if c else []


def whisper_cpu_threads() -> int:
    """cpu_threads for a whisper model loaded on the CPU (0 = its default)."""
    c = _cap("whisper_cpu_threads")
    return int(c) if c else 0
