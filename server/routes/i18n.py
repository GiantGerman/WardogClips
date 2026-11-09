"""UI language: the language list, the catalog bundle the page loads before it renders, and switching the language.

GET  /api/i18n/languages          {active, setting, system, languages: [{code, english, native, beta, coverage, ...}]}
GET  /api/i18n/bundle[?lang=]     {lang, chain, catalogs: {code: {"ns.key": text}}, languages, dev}
GET  /api/i18n/bundle.js[?lang=]  the same as a script (window.__WC_I18N__ = {...}); index.html loads it first
POST /api/i18n/language {language: "" | code}   saves ui.language and answers with the new bundle
"""

import json
import logging

from fastapi import APIRouter
from fastapi.responses import Response

from core import settings as settings_mod
from utils import i18n

logger = logging.getLogger("wclips.i18n")


def build(ctx) -> APIRouter:
    r = APIRouter()

    def _languages() -> dict:
        langs = i18n.public_languages()
        for x in langs:
            x["coverage"] = i18n.coverage(x["code"])
        return {"active": i18n.active_language(ctx.cfg), "setting": ctx.cfg.ui.language,
                "system": i18n.system_language(), "source": i18n.SOURCE, "languages": langs}

    @r.get("/api/i18n/languages")
    def languages():
        return _languages()

    @r.get("/api/i18n/bundle")
    def bundle(lang: str = ""):
        return i18n.bundle(lang or None, ctx.cfg)

    @r.get("/api/i18n/bundle.js")
    def bundle_js(lang: str = ""):
        data = json.dumps(i18n.bundle(lang or None, ctx.cfg), ensure_ascii=False, separators=(",", ":"))
        body = "window.__WC_I18N__=" + data.replace("</", "<\\/") + ";\n"
        return Response(body, media_type="application/javascript; charset=utf-8",
                        headers={"Cache-Control": "no-store"})

    @r.post("/api/i18n/language")
    def set_language(body: dict):
        want = str((body or {}).get("language") or "").strip()
        value = i18n.normalize_setting(want)
        if want and not value:
            raise i18n.app_error(400, "server.i18n_unknown_language", "This language is not available: {code}",
                                 code=want[:20])
        changed = settings_mod.apply_patch(ctx.cfg, {"ui": {"language": value}})
        if changed:
            ctx.save_settings()
            logger.info("[I18n] language set setting=%s changed=%s", value or "-", ",".join(changed))
            i18n.notify_change(i18n.active_language(ctx.cfg))
        out = _languages()
        out.update({"ok": True, "changed": changed, "bundle": i18n.bundle(None, ctx.cfg)})
        return out

    return r
