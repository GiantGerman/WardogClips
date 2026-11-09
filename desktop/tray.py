"""System-tray icon for `main.py --tray` (the installed app runs under pythonw.exe: no console window).

The tray owns the MAIN thread (pystray's win32 message loop wants it); uvicorn serves on a worker thread (main.py).
Menu: Open WardogClips (the default item = left click / double click), Update available... (only while a newer
release is offered: opens the page's update prompt), Pause while I play (= jobs.paused), Speed (the work mode,
core/workmode.py), Open data folder, Open log, Quit.
Quit calls the callback main.py hands in -- the same shutdown as POST /api/system/quit -- and that callback stops
the icon last (Tray.stop), which returns Tray.run() on the main thread.

Menu text is in the UI language (desktop/i18n/<lang>.json through utils/i18n.desktop_text; en-US built in) and
follows a language switch in the page (i18n.on_change -> the menu is rebuilt). Log lines stay English.

pystray + Pillow are runtime dependencies; `available()` lets main.py fall back to a plain foreground server when
they cannot be imported (logged). Nothing here touches the game or any other process.
"""

import logging
import os
import threading
from pathlib import Path

from utils import i18n, paths

logger = logging.getLogger("wclips.tray")

BALLOON_TITLE = "WardogClips"
BALLOON_TEXT = "WardogClips is running. Right-click the tray icon to quit."     # en-US source: desktop/i18n
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
    so a source checkout without the png still gets a recognizable icon."""
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
    from core.workmode import normal_priority
    try:
        with normal_priority():          # Explorer / the editor never inherit the work mode's low priority
            if p.is_dir() or p.exists():
                os.startfile(str(p))     # noqa: S606 - our own data dir / log file
            else:
                os.startfile(str(p.parent))
    except OSError as e:
        logger.warning("[Tray] could not open %s err=%s", p, e)


_WM_REFRESH_MENU = 0x0400 + 0x57          # WM_USER + n: our "rebuild the menu" message to the icon's window


class Tray:
    """One tray icon. `url()` -> the page to open; `on_quit()` runs the app's shutdown (it calls Tray.stop())."""

    def __init__(self, url, on_quit, balloon: bool | None = None, work=None):
        self._url = url
        self._on_quit = on_quit
        self._work = work                     # the app's Ctx (jobs + workmode); None = no pause / speed items
        self._work_key = None                 # (mode, paused) the menu's check marks last showed
        self._icon = None
        self._quitting = threading.Event()
        self._stopped = threading.Event()     # stop() before run() got the icon up (a server that never started)
        marker = paths.DATA_DIR / BALLOON_MARKER
        self._balloon = (not marker.exists()) if balloon is None else balloon
        self._marker = marker
        self._update = ""                     # the version offered right now ("" = no menu entry)
        self._notified = ""                   # one notification per offered version
        i18n.on_change(self._on_language)

    # -- text in the UI language -------------------------------------------------------------------------------
    def lang(self) -> str:
        try:
            return i18n.active_language(self._work.cfg)
        except AttributeError:
            return i18n.system_language()

    def tr(self, key: str, english: str = "", **params) -> str:
        return i18n.desktop_text(key, lang=self.lang(), english=english, **params)

    def _label(self, key: str, english: str):
        """pystray calls a callable text each time it builds the menu: the current language every time."""
        return lambda _item: self.tr(key, english)

    def _on_language(self, _code: str) -> None:
        self._refresh_menu()

    # -- menu actions ------------------------------------------------------------------------------------------
    def open_ui(self, *_):
        import webbrowser
        u = self._url()
        logger.info("[Tray] open url=%s", u)
        from core.workmode import normal_priority
        with normal_priority():
            webbrowser.open(u)

    def open_update(self, *_):
        import webbrowser
        u = self._url().split("#", 1)[0] + "#update"     # the page opens its update prompt for this hash
        logger.info("[Tray] open update prompt version=%s", self._update or "-")
        webbrowser.open(u)

    def set_update(self, view: dict) -> None:
        """core/updates.py listener: a newer release offered (prompt) -> menu entry + one notification."""
        ver = str(view.get("latest") or "") if view.get("prompt") or view.get("state") in ("downloading", "verifying") else ""
        if view.get("skipped"):
            ver = ""
        if ver == self._update:
            return
        self._update = ver
        icon = self._icon
        if icon is None:
            return
        try:
            icon.update_menu()
        except Exception:  # noqa: BLE001 - a menu refresh is a nicety
            pass
        if ver and ver != self._notified:
            self._notified = ver
            try:
                icon.notify(self.tr("tray.update_notify", "WardogClips {version} is available. Choose Update "
                                    "available... in the tray menu to see it.", version=ver), BALLOON_TITLE)
            except Exception as e:  # noqa: BLE001
                logger.info("[Tray] update notification not shown err=%s", e)

    def open_data(self, *_):
        _open_path(paths.DATA_DIR)

    def open_log(self, *_):
        _open_path(paths.LOG_FILE)

    # -- pause / work mode (the same calls as POST /api/system/workmode) -----------------------------------------------
    def is_paused(self, *_) -> bool:
        try:
            return bool(self._work.cfg.jobs.paused)
        except AttributeError:
            return False

    def toggle_pause(self, *_):
        w = self._work
        if w is None:
            return
        paused = not self.is_paused()
        w.jobs.set_paused(paused)
        w.save_settings()
        w.workmode.tick(force_publish=True, read=False)
        logger.info("[Tray] pause while I play=%s", paused)

    def mode_is(self, mode: str):
        def checked(*_):
            try:
                return self._work.workmode.mode == mode
            except AttributeError:
                return False
        return checked

    def set_mode(self, mode: str):
        def act(*_):
            w = self._work
            if w is None:
                return
            w.workmode.set_mode(mode)
            w.save_settings()
        return act

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
                icon.notify(self.tr("tray.balloon", BALLOON_TEXT), BALLOON_TITLE)
                self._marker.write_text("1", encoding="ascii")
            except Exception as e:  # noqa: BLE001 - a balloon is a nicety
                logger.info("[Tray] balloon not shown err=%s", e)

    def run(self) -> None:
        """Blocks on the calling (main) thread until stop()."""
        import pystray
        if self._stopped.is_set():
            return
        menu = pystray.Menu(
            pystray.MenuItem(self._label("tray.open", "Open WardogClips"), self.open_ui, default=True),
            pystray.MenuItem(self._label("tray.update", "Update available..."), self.open_update,
                             visible=lambda item: bool(self._update)),
            *self._work_items(pystray),
            pystray.MenuItem(self._label("tray.open_data", "Open data folder"), self.open_data),
            pystray.MenuItem(self._label("tray.open_log", "Open log"), self.open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._label("tray.quit", "Quit"), self.quit),
        )
        self._icon = pystray.Icon("WardogClips", icon_image(), f"WardogClips v{paths.APP_VERSION}", menu)
        logger.info("[Tray] icon up")
        self._icon.run(setup=self._setup)
        logger.info("[Tray] icon gone")

    def _work_items(self, pystray) -> list:
        if self._work is None:
            return []
        try:
            if self._on_work_change not in self._work.workmode.listeners:
                self._work.workmode.listeners.append(self._on_work_change)
        except AttributeError:
            pass
        speed = pystray.Menu(
            pystray.MenuItem(self._label("tray.mode_full", "Full speed"), self.set_mode("full"),
                             checked=self.mode_is("full"), radio=True),
            pystray.MenuItem(self._label("tray.mode_balanced", "Balanced"), self.set_mode("balanced"),
                             checked=self.mode_is("balanced"), radio=True),
            pystray.MenuItem(self._label("tray.mode_background", "Background"), self.set_mode("background"),
                             checked=self.mode_is("background"), radio=True),
        )
        return [pystray.Menu.SEPARATOR,
                pystray.MenuItem(self._label("tray.pause", "Pause while I play"), self.toggle_pause,
                                 checked=self.is_paused),
                pystray.MenuItem(self._label("tray.speed", "Speed"), speed),
                pystray.Menu.SEPARATOR]

    def _on_work_change(self, st: dict) -> None:
        """A mode / pause change made in the page (or anywhere): the tray's check marks follow."""
        key = (st.get("mode"), st.get("paused"))
        if key == self._work_key:
            return
        self._work_key = key
        self._refresh_menu()

    def _refresh_menu(self) -> None:
        """Rebuild the menu on the icon's own thread (a posted window message; pystray builds menus there)."""
        icon = self._icon
        hwnd = getattr(icon, "_hwnd", None)
        handlers = getattr(icon, "_message_handlers", None)
        if not hwnd or not isinstance(handlers, dict):
            return
        try:
            from pystray._util import win32 as pw32
            handlers.setdefault(_WM_REFRESH_MENU, lambda w, l: icon.update_menu())
            pw32.PostMessage(hwnd, _WM_REFRESH_MENU, 0, 0)
        except Exception as e:  # noqa: BLE001 - stale check marks are the worst case
            logger.debug("[Tray] menu refresh not posted err=%s", e)

    def stop(self) -> None:
        self._stopped.set()
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001 - already stopped
                pass
