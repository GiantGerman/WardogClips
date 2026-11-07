"""System-tray icon for `main.py --tray` (the installed app runs under pythonw.exe: no console window).

The tray owns the MAIN thread (pystray's win32 message loop wants it); uvicorn serves on a worker thread (main.py).
Menu: Open WardogClips (the default item = left click / double click), Open data folder, Open log, Quit.
Quit calls the callback main.py hands in -- the same shutdown as POST /api/system/quit -- and that callback stops
the icon last (Tray.stop), which returns Tray.run() on the main thread.

pystray + Pillow are runtime dependencies; `available()` lets main.py fall back to a plain foreground server when
they cannot be imported (logged). Nothing here touches the game or any other process.
"""

import logging
import os
import threading
from pathlib import Path

from utils import paths

logger = logging.getLogger("wclips.tray")

BALLOON_TITLE = "WardogClips"
BALLOON_TEXT = "WardogClips is running -- right-click the tray icon to quit"
BALLOON_MARKER = "tray_hint_shown"        # in DATA_DIR: the balloon is a first-run hint, shown once per data dir


def available() -> tuple[bool, str]:
    """(True, "") when pystray + Pillow import; else (False, why) for the startup log."""
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
        return True, ""
    except Exception as e:  # noqa: BLE001 - any import trouble means the console fallback
        return False, f"{type(e).__name__}: {e}"


def icon_image(size: int = 64):
    """The app icon: web/img/icon-192.png when the build made one, else a drawn mark (dark rounded square, accent W)
    so a source checkout without the png still gets a recognisable icon."""
    from PIL import Image, ImageDraw
    png = paths.WEB_DIR / "img" / "icon-192.png"
    if png.is_file():
        try:
            return Image.open(png).convert("RGBA").resize((size, size), Image.LANCZOS)
        except OSError:
            pass
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = size // 6
    d.rounded_rectangle((1, 1, size - 2, size - 2), radius=r, fill=(24, 27, 33, 255))
    w = max(2, size // 10)
    m = size // 5
    pts = [(m, m + 2), (m + (size - 2 * m) // 4, size - m), (size // 2, size // 2 - 2),
           (size - m - (size - 2 * m) // 4, size - m), (size - m, m + 2)]
    d.line(pts, fill=(255, 140, 40, 255), width=w, joint="curve")
    return img


def _open_path(p: Path) -> None:
    """Explorer / the default .log handler; os.startfile never makes a console."""
    try:
        if p.is_dir() or p.exists():
            os.startfile(str(p))         # noqa: S606 - our own data dir / log file
        else:
            os.startfile(str(p.parent))
    except OSError as e:
        logger.warning("[Tray] could not open %s err=%s", p, e)


class Tray:
    """One tray icon. `url()` -> the page to open; `on_quit()` runs the app's shutdown (it calls Tray.stop())."""

    def __init__(self, url, on_quit, balloon: bool | None = None):
        self._url = url
        self._on_quit = on_quit
        self._icon = None
        self._quitting = threading.Event()
        self._stopped = threading.Event()     # stop() before run() got the icon up (a server that never started)
        marker = paths.DATA_DIR / BALLOON_MARKER
        self._balloon = (not marker.exists()) if balloon is None else balloon
        self._marker = marker

    # -- menu actions ------------------------------------------------------------------------------------------
    def open_ui(self, *_):
        import webbrowser
        u = self._url()
        logger.info("[Tray] open url=%s", u)
        webbrowser.open(u)

    def open_data(self, *_):
        _open_path(paths.DATA_DIR)

    def open_log(self, *_):
        _open_path(paths.LOG_FILE)

    def quit(self, *_):
        if self._quitting.is_set():
            return
        self._quitting.set()
        logger.info("[Tray] quit chosen")
        # the shutdown joins the server and stops the lanes: never on pystray's message thread (the menu would hang)
        threading.Thread(target=self._on_quit, name="tray-quit", daemon=True).start()

    # -- lifecycle ---------------------------------------------------------------------------------------------
    def _setup(self, icon):
        icon.visible = True
        if self._stopped.is_set():
            icon.stop()
            return
        if self._balloon:
            try:
                icon.notify(BALLOON_TEXT, BALLOON_TITLE)
                self._marker.write_text("1", encoding="ascii")
            except Exception as e:  # noqa: BLE001 - a balloon is a nicety
                logger.info("[Tray] balloon not shown err=%s", e)

    def run(self) -> None:
        """Blocks on the calling (main) thread until stop()."""
        import pystray
        if self._stopped.is_set():
            return
        menu = pystray.Menu(
            pystray.MenuItem("Open WardogClips", self.open_ui, default=True),
            pystray.MenuItem("Open data folder", self.open_data),
            pystray.MenuItem("Open log", self.open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self.quit),
        )
        self._icon = pystray.Icon("WardogClips", icon_image(), f"WardogClips v{paths.APP_VERSION}", menu)
        logger.info("[Tray] icon up")
        self._icon.run(setup=self._setup)
        logger.info("[Tray] icon gone")

    def stop(self) -> None:
        self._stopped.set()
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001 - already stopped
                pass
