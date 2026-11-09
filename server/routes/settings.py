"""GET/POST /api/settings -- partial merges per section, autosaved by the drawer."""

import logging

from fastapi import APIRouter

from core import settings as settings_mod
from utils.i18n import app_error

logger = logging.getLogger("wclips.settings")


def settings_view(cfg) -> dict:
    """settings.to_dict + the "choices" lists the page's selects need (also answered by POST /api/setup)."""
    d = settings_mod.to_dict(cfg)
    d["choices"] = {"stt_models": list(settings_mod.STT_MODELS), "stt_compute": list(settings_mod.STT_COMPUTE),
                    "proxy_codecs": list(settings_mod.PROXY_CODECS), "ocr_devices": list(settings_mod.OCR_DEVICES), "marker_colors": list(settings_mod.MARKER_COLORS)}
    return d


def build(ctx) -> APIRouter:
    r = APIRouter()

    def _view() -> dict:
        return settings_view(ctx.cfg)

    @r.get("/api/settings")
    def get_settings():
        return _view()

    @r.post("/api/settings")
    def post_settings(body: dict):
        if not isinstance(body, dict):
            raise app_error(400, "server.bad_request", "The request was not understood.")
        changed = settings_mod.apply_patch(ctx.cfg, body)
        ctx.save_settings()
        if changed:
            logger.info("[Settings] changed=%s", ",".join(changed))
        if "server.verbose_console" in changed:
            from utils.logging_config import set_console_verbose
            set_console_verbose(bool(ctx.cfg.server.verbose_console))
        if "jobs.paused" in changed:
            ctx.jobs.set_paused(ctx.cfg.jobs.paused)
        if any(c.startswith("performance.") or c == "jobs.paused" for c in changed) and getattr(ctx, "workmode", None):
            ctx.workmode.tick(force_publish=True, read=False)       # the status-bar chip follows at once
        if any(c.startswith("library.") for c in changed):
            ctx.bus.publish("library", action="settings")
        if "ui.language" in changed:
            from utils import i18n
            i18n.notify_change(i18n.active_language(ctx.cfg))     # the tray menu follows
        if any(c in ("stt.model", "stt.compute") for c in changed):
            from stt import whisper as stt
            stt.engine.unload()         # the next transcribe job loads the new model
        return {"ok": True, "changed": changed, "settings": _view()}

    return r
