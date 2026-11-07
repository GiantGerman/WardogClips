"""WardogClips logging -- console + file, an operator-console format.

Design:
- ONE format for console and file:
      %(asctime)s | %(levelname)-5s | %(rec)-10s | %(name)-18s | %(message)s
  The context column is the recording the current thread is working on.
- Console = the INFO stream on stderr; file = INFO + DEBUG.
- ColorFormatter colors ONLY the level name, padded inside the escape codes so
  column alignment survives; gated on a real TTY and NO_COLOR /
  WARDOGCLIPS_LOG_NO_COLOR / WARDOGCLIPS_LOG_JSON.
- Safe handlers: a broken stream never cascades tracebacks.
- Third-party loggers are capped at the logger level (console AND file).

House line shape for INFO+: ``[Component] message key=value``; ASCII only.
Debug tier: ``WARDOGCLIPS_DEBUG=1`` or ``--debug`` lifts the FILE handler to DEBUG
(the console stays INFO unless ``--verbose`` / Settings > System > verbose console).

Tray mode (pythonw.exe): there is no console -- ``sys.stdout`` / ``sys.stderr`` are None. The console handler
is then never attached (``setup_logging(console=False)`` or a None stream), so logging goes to the file only.
"""

import json
import logging
import logging.handlers
import os
import sys
import threading

from utils import paths

_ENV_PREFIX = "WARDOGCLIPS"
_FORMAT = "%(asctime)s | %(levelname)-5s | %(rec)-10s | %(name)-18s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_ANSI_RESET = "\033[0m"
_ANSI_BOLD = "\033[1m"
_ANSI_GRAY = "\033[90m"
_ANSI_CYAN = "\033[36m"
_ANSI_BRIGHT_YELLOW = "\033[93m"
_ANSI_BRIGHT_RED = "\033[91m"
_LEVEL_COLORS = {
    "DEBUG": _ANSI_GRAY,
    "INFO": _ANSI_CYAN,
    "WARNING": _ANSI_BRIGHT_YELLOW + _ANSI_BOLD,
    "ERROR": _ANSI_BRIGHT_RED + _ANSI_BOLD,
    "CRITICAL": _ANSI_BRIGHT_RED + _ANSI_BOLD,
}

_THIRD_PARTY_LEVELS = (
    ("urllib3", logging.ERROR),
    ("requests", logging.WARNING),
    ("uvicorn", logging.WARNING), ("uvicorn.error", logging.WARNING),
    ("uvicorn.access", logging.WARNING),
    ("asyncio", logging.WARNING), ("PIL", logging.WARNING),
    ("httpx", logging.WARNING), ("httpcore", logging.WARNING),
    ("faster_whisper", logging.WARNING), ("ctranslate2", logging.WARNING),
    ("numba", logging.WARNING), ("watchfiles", logging.WARNING),
)

_configured = False
_console_handler = None
_file_handler: logging.Handler | None = None
_local = threading.local()


def set_context(rec: str | None = None) -> None:
    """Stamp a recording label onto this thread's subsequent records ('' clears)."""
    _local.rec = (rec or "")[:10]


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.rec = getattr(_local, "rec", "")
        return True


_LEVEL_SHORT = {"WARNING": "WARN", "CRITICAL": "CRIT"}


class PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        original = record.levelname
        record.levelname = _LEVEL_SHORT.get(original, original)
        try:
            return super().format(record)
        finally:
            record.levelname = original


class ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        original = record.levelname
        color = _LEVEL_COLORS.get(original, "")
        short = _LEVEL_SHORT.get(original, original)
        record.levelname = f"{color}{short:<5}{_ANSI_RESET}" if color else short
        try:
            return super().format(record)
        finally:
            record.levelname = original


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": self.formatTime(record, _DATE_FORMAT),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if getattr(record, "rec", ""):
            out["rec"] = record.rec
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=True)


class _SafeStreamHandler(logging.StreamHandler):
    def handleError(self, record) -> None:      # noqa: N802
        pass

    def flush(self) -> None:
        try:
            super().flush()
        except OSError:
            pass


ROLLOVER_RETRY_S = 300          # a rollover that failed (another process has the log open) is tried again after this


class _SafeTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    def handleError(self, record) -> None:      # noqa: N802
        pass

    def doRollover(self) -> None:                # noqa: N802
        """The midnight rename fails on Windows while another process has the log open (Python opens files without
        FILE_SHARE_DELETE). The stock handler then left rolloverAt in the past and every later record retried the
        rollover and was dropped. Now: keep writing to the current file and try again in ROLLOVER_RETRY_S."""
        try:
            super().doRollover()
        except OSError as e:
            import time as _time
            if self.stream is None:
                try:
                    self.stream = self._open()
                except OSError:
                    pass
            self.rolloverAt = int(_time.time()) + ROLLOVER_RETRY_S
            if self.stream is not None:
                try:
                    self.stream.write(f"[Log] rollover failed, still writing here err={type(e).__name__}"
                                      f" retry_s={ROLLOVER_RETRY_S}\n")
                except OSError:
                    pass

    def flush(self) -> None:
        try:
            super().flush()
        except OSError:
            pass


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _json_mode() -> bool:
    return _env_flag(f"{_ENV_PREFIX}_LOG_JSON")


def _console_supports_color() -> bool:
    if _env_flag(f"{_ENV_PREFIX}_LOG_NO_COLOR") or os.environ.get("NO_COLOR", "").strip():
        return False
    if _json_mode():
        return False
    try:
        return sys.stderr is not None and bool(sys.stderr.isatty())
    except Exception:
        return False


def _enable_ansi_on_windows() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        for std in (-11, -12):
            handle = kernel32.GetStdHandle(std)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def has_console() -> bool:
    """A usable stderr (False under pythonw.exe, where sys.stderr is None, or once main.py swapped in os.devnull)."""
    return sys.stderr is not None and getattr(sys.stderr, "name", "") != os.devnull


def setup_logging(debug: bool | None = None, console: bool = True) -> None:
    """Configure root logging once. Safe to call repeatedly. console=False (tray mode) = file only."""
    global _configured, _file_handler
    if _configured:
        if debug is not None:
            set_debug(debug)
        return
    paths.ensure_dirs()
    if debug is None:
        debug = _env_flag(f"{_ENV_PREFIX}_DEBUG")

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    ctx = _ContextFilter()

    if _json_mode():
        console_fmt: logging.Formatter = JSONFormatter()
        file_fmt: logging.Formatter = JSONFormatter()
    else:
        file_fmt = PlainFormatter(_FORMAT, _DATE_FORMAT)
        if _console_supports_color():
            _enable_ansi_on_windows()
            console_fmt = ColorFormatter(_FORMAT, _DATE_FORMAT)
        else:
            console_fmt = file_fmt

    _file_handler = _SafeTimedRotatingFileHandler(
        paths.LOG_FILE, when="midnight", interval=1, backupCount=14, encoding="utf-8"
    )
    _file_handler.setFormatter(file_fmt)
    _file_handler.setLevel(logging.DEBUG if debug else logging.INFO)
    _file_handler.addFilter(ctx)
    root.addHandler(_file_handler)

    global _console_handler
    if console and has_console():
        h = _SafeStreamHandler(sys.stderr)
        h.setFormatter(console_fmt)
        h.setLevel(logging.DEBUG if (debug or _env_flag(f"{_ENV_PREFIX}_VERBOSE")) else logging.INFO)
        h.addFilter(ctx)
        root.addHandler(h)
        _console_handler = h

    def _excepthook(exc_type, exc, tb):
        logging.getLogger("wclips.fatal").critical(
            "[Fatal] uncaught exception", exc_info=(exc_type, exc, tb))
        if has_console():                    # pythonw: no stderr to print the traceback on (the file has it)
            sys.__excepthook__(exc_type, exc, tb)

    def _thread_excepthook(args):
        logging.getLogger("wclips.fatal").critical(
            "[Fatal] thread=%s uncaught exception",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    for name, lvl in _THIRD_PARTY_LEVELS:
        logging.getLogger(name).setLevel(lvl)
    _configured = True
    root.info("Logging initialized - file: %s", paths.LOG_FILE)


def set_debug(enabled: bool) -> None:
    if _file_handler is not None:
        _file_handler.setLevel(logging.DEBUG if enabled else logging.INFO)
        for name, lvl in _THIRD_PARTY_LEVELS:
            logging.getLogger(name).setLevel(lvl)
        logging.getLogger("wclips").info("[Log] debug=%s", enabled)
    if enabled:
        set_console_verbose(True)


def set_console_verbose(enabled: bool) -> None:
    """DEBUG lines on the console as well (Settings > System > verbose console)."""
    if _console_handler is not None:
        _console_handler.setLevel(logging.DEBUG if enabled else logging.INFO)
        logging.getLogger("wclips").info("[Log] console=%s", "DEBUG" if enabled else "INFO")


def console_level() -> str:
    return logging.getLevelName(_console_handler.level) if _console_handler is not None else "none"


def is_debug() -> bool:
    return _file_handler is not None and _file_handler.level == logging.DEBUG


def rule(width: int = 45) -> str:
    """Horizontal rule for banners (ASCII, per house rule)."""
    return "-" * width
