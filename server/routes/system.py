"""/api/status (the reopen guard, the launcher's single-instance check and the UI header read this),
logs, hardware, and POST /api/system/quit (the installer / `WardogClips.exe --quit` / stop_wardogclips.ps1
stop a running instance through it)."""

import logging
import os
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from media import ffmpeg
from utils import paths
from utils.logging_config import console_level

logger = logging.getLogger("wclips.system")

LOCAL_CLIENTS = ("127.0.0.1", "::1", "localhost")
QUIT_DELAY_S = 0.3          # the quit answer must reach the caller before the server stops listening


def is_local(request: Request) -> bool:
    """Only this machine may stop the app (uvicorn binds 127.0.0.1 by default, but server.host may be 0.0.0.0)."""
    host = request.client.host if request.client else ""
    return host in LOCAL_CLIENTS


def build(ctx) -> APIRouter:
    r = APIRouter()

    @r.get("/api/status")
    def status():
        return {
            "app": "WardogClips", "version": paths.APP_VERSION,
            "setup_done": bool(ctx.cfg.setup.done), "installed": paths.INSTALLED,
            "pid": os.getpid(), "port": getattr(ctx, "port", None), "mode": getattr(ctx, "run_mode", "console"),
            "data_dir": str(paths.DATA_DIR), "data_dir_reason": paths.DATA_DIR_REASON,
            "log_file": str(paths.LOG_FILE), "console_level": console_level(),
            "uptime_s": round(time.time() - ctx.started_utc, 1),
            "library": ctx.library.summary(),
            "jobs": ctx.jobs.counts(),
            "running": [{"id": j["id"], "stage": j["stage"], "rec_id": j["rec_id"]} for j in ctx.jobs.running.values()],
            "events": {"subscribers": ctx.bus.subscribers, "published": ctx.bus.published},
            "ffmpeg": {"found": bool(paths.ffmpeg_exe()), "ffprobe": bool(paths.ffprobe_exe())},
        }

    @r.post("/api/system/quit")
    def quit_app(request: Request):
        """Stop this instance like the tray's Quit (lanes stopped, running jobs back to queued, instance.json
        removed). 127.0.0.1 only. The shutdown starts QUIT_DELAY_S after the answer so the caller gets it."""
        if not is_local(request):
            return JSONResponse({"ok": False, "error": "only this computer can stop WardogClips"}, status_code=403)
        cb = getattr(ctx, "request_quit", None)
        if cb is None:
            return JSONResponse({"ok": False, "error": "this instance has no quit hook (tests / --scan)"}, status_code=503)
        logger.info("[System] quit requested over HTTP client=%s", request.client.host if request.client else "?")

        def _later():
            time.sleep(QUIT_DELAY_S)
            try:
                cb()
            except Exception:  # noqa: BLE001 - logged; the caller already has its answer
                logger.exception("[System] quit callback failed")
        threading.Thread(target=_later, name="http-quit", daemon=True).start()
        return {"ok": True, "mode": getattr(ctx, "run_mode", "console"), "pid": os.getpid()}

    @r.get("/api/system/hw")
    def hw():
        return ffmpeg.hw_info()

    @r.get("/api/system/encoder")
    def encoder_status():
        """What 'auto' encodes with right now (NVENC sessions open on the GPU vs ours) -- media/encoder.py."""
        from media import encoder
        out = encoder.status()
        out["clip_setting"], out["proxy_setting"] = ctx.cfg.proxies.clip_codec, ctx.cfg.proxies.codec
        # what would actually run now (an NVENC setting falls back to x264 when this ffmpeg lacks NVENC)
        out["clip_effective"] = encoder.choose(ctx.cfg.proxies.clip_codec)[0]
        out["proxy_effective"] = encoder.choose(ctx.cfg.proxies.codec)[0]
        return out

    @r.get("/api/system/logs")
    def logs(tail: int = 120):
        try:
            lines = paths.LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        return PlainTextResponse("\n".join(lines[-max(1, min(tail, 2000)):]))

    return r
