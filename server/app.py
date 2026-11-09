"""FastAPI app factory: routes + static. The page is web/index.html; /static is the
web dir. ``Ctx`` is the one object every route and stage shares."""

import logging
import time

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from utils import paths

logger = logging.getLogger("wclips.server")


class Ctx:
    def __init__(self, cfg, db, bus, library, jobs, resolve_link=None):
        self.cfg = cfg
        self.db = db
        self.bus = bus
        self.library = library
        self.jobs = jobs
        if resolve_link is None:
            from resolve.link import ResolveLink
            resolve_link = ResolveLink(cfg, bus)
        self.resolve_link = resolve_link
        from core.workmode import WorkMode
        self.workmode = WorkMode(cfg, bus)     # work modes: Full speed / Balanced / Background (core/workmode.py)
        self.started_utc = time.time()
        # main.py registers how this process shuts down (tray Quit, POST /api/system/quit); None = no hook
        # (tests, --scan). run_mode = "console" | "tray" for /api/status.
        self.request_quit = None
        self.run_mode = "console"
        self.port = None
        from core.updates import Updater
        self.updater = Updater(self)           # the automatic check starts only in main.py (server runs)

    @classmethod
    def build(cls, cfg) -> "Ctx":
        from core.db import DB
        from core.events import bus
        from core.jobs import JobQueue
        from core.library import Library
        from core import stages
        db = DB(paths.DB_FILE)
        jobs = JobQueue(cfg, db, bus)
        stages.register_all(jobs)
        library = Library(cfg, db, bus, enqueue_probe=lambda rec_id, priority=0: jobs.enqueue(rec_id, "probe", priority))
        from media import vods
        jobs.register("download", lambda jc: vods.run_download(jc, library))    # stream VODs -> the library
        return cls(cfg, db, bus, library, jobs)

    def save_settings(self) -> None:
        from core import settings as settings_mod
        settings_mod.save(self.cfg)

    def start(self) -> None:
        """Background work: recover crashed jobs, start the lanes and the library watcher, and get
        DaVinci Resolve up with a project (Settings > Resolve > launch_on_start; never in --scan)."""
        self.jobs.recover()
        try:
            from core import checkpoints
            waiting = {(r["stage"], int(r["rec_id"])) for r in self.db.query(
                "SELECT stage, rec_id FROM jobs WHERE state IN ('queued', 'running') AND rec_id IS NOT NULL")}
            checkpoints.sweep(keep=waiting)    # resume points nobody came back to in 14 days (+ their wavs)
        except Exception:                      # noqa: BLE001
            logger.exception("[Checkpoint] sweep failed")
        self._start_workmode()
        self.jobs.start()
        self.library.start_watch()
        try:
            self.resolve_link.startup()
        except Exception:                      # noqa: BLE001 -- Resolve must never stop WardogClips from starting
            logger.exception("[Resolve] startup hand-shake failed")

    def stop(self) -> None:
        self.updater.stop()
        self.resolve_link.stop()
        self.library.stop_watch()
        self.jobs.stop()
        self.workmode.stop()

    def _start_workmode(self) -> None:
        """A mode / busy change wakes the lanes (a claim that waited may go now) and re-sizes the OCR pools."""
        def on_change(_state):
            self.jobs._size_ocr_pool()
            self.jobs._wake.set()
        self.workmode.listeners.append(on_change)
        try:
            self.workmode.start()
        except Exception:                      # noqa: BLE001 -- never stop WardogClips from starting
            logger.exception("[WorkMode] monitor did not start")


# ---- request guard (CSRF + DNS rebinding) ------------------------------------------------------------------------------
# The client IP is always 127.0.0.1 when a web page in the user's own browser calls us, so it proves nothing:
# - Host must name this machine by a loopback name (any port): a DNS-rebinding page sends ITS domain -> 421.
# - a present Origin must be http://<loopback name>:<our port> -> else 403 (a cross-site fetch, Origin "null").
# - every non-GET /api/* request (and GET /api/system/check-folder, which touches the filesystem) must carry
#   X-WardogClips: 1. A custom header forces a CORS preflight, which this server never approves, so a cross-site
#   page cannot send it; the page's api()/post()/del()/put(), the launcher and the stop script do.
#   Sec-Fetch-Site cross-site / same-site is refused there as well (belt and braces).
# /api/stream (EventSource) and the static / media GETs need only the Host + Origin checks.
CSRF_HEADER = "x-wardogclips"
CSRF_VALUE = "1"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::", "[::]"})
EXTRA_HOSTS: set[str] = set()          # tests add "testserver" (Starlette's TestClient); empty in the app
HEADER_GETS = frozenset({"/api/system/check-folder"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _ascii(s, n: int = 80) -> str:
    return str(s or "")[:n].encode("ascii", "replace").decode()


def split_host(value: str) -> tuple[str, int | None]:
    """'127.0.0.1:8558' -> ('127.0.0.1', 8558); '[::1]:80' -> ('[::1]', 80); 'localhost' -> ('localhost', None).
    The name comes back lower case; a malformed port -> ('', None)."""
    v = (value or "").strip().lower()
    if v.startswith("["):
        end = v.find("]")
        if end < 0:
            return "", None
        name, rest = v[:end + 1], v[end + 1:]
    elif v.count(":") == 1:
        name, rest = v.split(":", 1)
        rest = ":" + rest
    elif ":" in v:                       # a bare IPv6 literal is not a valid Host header
        return "", None
    else:
        name, rest = v, ""
    if not rest:
        return name, None
    if not rest.startswith(":") or not rest[1:].isdigit():
        return "", None
    return name, int(rest[1:])


def allowed_hosts(request: Request, ctx) -> set[str]:
    hosts = set(LOOPBACK_HOSTS) | EXTRA_HOSTS
    try:
        conf = str(ctx.cfg.server.host or "").strip().lower()
    except AttributeError:
        conf = ""
    if conf in WILDCARD_HOSTS:
        # LAN mode (bound to every interface): the IP literal the connection came in on names this machine too.
        # A rebinding page always sends its own DOMAIN, never our address, so this does not reopen that hole.
        if conf:
            srv = request.scope.get("server") or ()
            if srv and srv[0]:
                ip = str(srv[0]).lower()
                hosts.add(f"[{ip}]" if ":" in ip else ip)
    else:
        hosts.add(f"[{conf}]" if ":" in conf and not conf.startswith("[") else conf)
    return hosts


def _server_port(request: Request, ctx) -> int | None:
    if getattr(ctx, "port", None):
        return int(ctx.port)
    srv = request.scope.get("server") or ()
    return int(srv[1]) if len(srv) > 1 and srv[1] else None


def request_refusal(request: Request, ctx) -> tuple[int, str] | None:
    """None = let it through; else (status, reason). Pure function of the request: tests call it directly too."""
    hosts = allowed_hosts(request, ctx)
    name, _port = split_host(request.headers.get("host", ""))
    if not name or name not in hosts:
        return 421, f"host {_ascii(request.headers.get('host', '-'), 60)!s} is not this computer"
    origin = request.headers.get("origin")
    if origin is not None:
        o = origin.strip().lower()
        ok = False
        if o.startswith("http://"):
            oname, oport = split_host(o[len("http://"):])
            want = _server_port(request, ctx)
            ok = bool(oname) and oname in hosts and (oport or 80) == (want or 80) and "/" not in o[len("http://"):]
        if not ok:
            return 403, f"cross-site origin {_ascii(origin, 60)}"
    path = request.url.path
    if (request.method not in SAFE_METHODS and path.startswith("/api/")) or (path in HEADER_GETS and request.method != "OPTIONS"):
        site = (request.headers.get("sec-fetch-site") or "").strip().lower()
        if site in ("cross-site", "same-site"):
            return 403, f"sec-fetch-site {_ascii(site, 20)}"
        if (request.headers.get(CSRF_HEADER) or "").strip() != CSRF_VALUE:
            return 403, "missing X-WardogClips header"
    return None


def create_app(ctx: Ctx) -> FastAPI:
    from server.routes import (detect, drives, events, jobs, learning, library, player, resolve, settings, setup,
                               system, transcript, updates, vodbrowse, vods, zones)
    from server.routes import i18n as i18n_routes
    from server.routes import makeclip as makeclip_routes
    from utils.i18n import AppError

    app = FastAPI(title="WardogClips", version=paths.APP_VERSION)

    # the <video> player aborts range requests whenever it seeks or switches clips; on Windows the
    # proactor loop then logs "ConnectionResetError [WinError 10054]" with a traceback at ERROR. Harmless.
    def _drop_resets(record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not isinstance(exc, (ConnectionResetError, ConnectionAbortedError))
    logging.getLogger("asyncio").addFilter(_drop_resets)

    # registered BEFORE _timing, so _timing is the outer one and logs the refusals with their status
    @app.middleware("http")
    async def _guard(request: Request, call_next):
        why = request_refusal(request, ctx)
        if why is not None:
            status, reason = why
            logger.info("[Security] refused method=%s path=%s status=%d reason=%s", request.method,
                        _ascii(request.url.path, 120), status, reason)
            return JSONResponse({"detail": "refused: " + reason}, status_code=status)
        return await call_next(request)

    @app.middleware("http")
    async def _timing(request: Request, call_next):
        t0 = time.perf_counter()
        resp = await call_next(request)
        ms = (time.perf_counter() - t0) * 1000.0
        path = request.url.path
        if path.startswith("/api/") and path != "/api/stream" and (ms > 250 or resp.status_code >= 400):
            logger.info("[HTTP] %s %s %d %.0fms", request.method, path, resp.status_code, ms)
        if path.startswith("/static") or path == "/":
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    # translatable errors (utils/i18n.py AppError): "detail" stays the English text, "code" + "params" let the page
    # show the catalog text instead
    @app.exception_handler(AppError)
    async def _app_error(_request: Request, exc: AppError):
        return JSONResponse({"detail": exc.detail, "code": exc.code, "params": exc.params},
                            status_code=exc.status_code, headers=getattr(exc, "headers", None))

    # two routers must never claim the same method + path (the later one would silently shadow the earlier)
    seen: dict[tuple, str] = {}
    for mod in (system, settings, library, jobs, transcript, detect, player, resolve, learning, drives, events, vods,
                vodbrowse, zones, setup, makeclip_routes, updates, i18n_routes):
        router = mod.build(ctx)
        for route in router.routes:
            for m in getattr(route, "methods", None) or ():
                key = (m, getattr(route, "path", ""))
                if key in seen:
                    raise RuntimeError(f"duplicate route {m} {key[1]} ({seen[key]} and {mod.__name__})")
                seen[key] = mod.__name__
        app.include_router(router)

    paths.CROPS_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/crops", StaticFiles(directory=paths.CROPS_DIR), name="crops")

    @app.get("/")
    def index():
        f = paths.WEB_DIR / "index.html"
        if f.exists():
            return FileResponse(f)
        return JSONResponse({"app": "WardogClips", "hint": "web/index.html missing"})

    if paths.WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=paths.WEB_DIR), name="static")
    return app
