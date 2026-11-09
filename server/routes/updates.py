"""Update check routes (core/updates.py does the work; the page also gets every state change as SSE `updates`).

GET  /api/updates            the current state; ?force=1 checks now (Settings > Updates "Check now")
POST /api/updates/check      check now
POST /api/updates/later      hide the prompt until the next check
POST /api/updates/skip       {version?} never offer this version again
POST /api/updates/install    {confirm_unverified?} download, verify, start the installer, quit (installed builds)
POST /api/updates/cancel     stop a running download
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from utils.i18n import with_msg

logger = logging.getLogger("wclips.updates")

LOCAL_CLIENTS = ("127.0.0.1", "::1", "localhost")


def _updater(ctx):
    up = getattr(ctx, "updater", None)
    if up is None:
        from core.updates import Updater
        up = ctx.updater = Updater(ctx)
    return up


def build(ctx) -> APIRouter:
    r = APIRouter()

    @r.get("/api/updates")
    def get_updates(force: int = 0):
        up = _updater(ctx)
        return up.check("manual") if force else up.view()

    @r.post("/api/updates/check")
    def check_now():
        return _updater(ctx).check("manual")

    @r.post("/api/updates/later")
    def later():
        return _updater(ctx).later()

    @r.post("/api/updates/skip")
    def skip(body: dict | None = None):
        body = body if isinstance(body, dict) else {}
        return _updater(ctx).skip(str(body.get("version") or ""))

    @r.post("/api/updates/install")
    def install(request: Request, body: dict | None = None):
        # it starts a program on this PC and stops the app: this machine only (like POST /api/system/quit)
        host = request.client.host if request.client else ""
        if host not in LOCAL_CLIENTS:
            return JSONResponse(with_msg({"ok": False, "code": "local_only"}, "reason", "server.update.local_only",
                                         "Updates can be installed from this computer only."), status_code=403)
        body = body if isinstance(body, dict) else {}
        res = _updater(ctx).install(confirm_unverified=bool(body.get("confirm_unverified")))
        if not res.get("ok"):
            return JSONResponse(res, status_code=409)
        return res

    @r.post("/api/updates/cancel")
    def cancel():
        return {"ok": _updater(ctx).cancel()}

    return r
