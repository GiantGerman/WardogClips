"""Transcripts: read one recording's segments, search across all of them, queue
transcription, report the speech-to-text engine."""

import logging

from fastapi import APIRouter

from core import settings as settings_mod
from core import speechlang, transcripts
from core.library import parse_name, _priority
from utils.i18n import app_error

logger = logging.getLogger("wclips.transcripts")


def build(ctx) -> APIRouter:
    r = APIRouter()

    def _rec(rec_id: int) -> dict:
        rec = ctx.db.one("SELECT * FROM recordings WHERE id=?", (rec_id,))
        if rec is None:
            raise app_error(404, "server.no_recording", "That recording does not exist anymore.")
        return rec

    def _queue(rec: dict, after_running: bool = False) -> int | None:
        kind, name_utc = parse_name(rec["name"])
        return ctx.jobs.enqueue(rec["id"], "transcribe", priority=_priority(name_utc, rec["mtime"]),
                                after_running=after_running)

    @r.get("/api/recordings/{rec_id}/transcript")
    def transcript(rec_id: int, words: int = 0):
        _rec(rec_id)
        return {"rec_id": rec_id, "segments": transcripts.segments(ctx.db, rec_id, with_words=bool(words)),
                "summary": transcripts.summary(ctx.db, rec_id)}

    @r.get("/api/search")
    def search(q: str = "", limit: int = 50, drive: str = ""):
        return transcripts.search(ctx.db, q, limit=limit, drive=drive)

    @r.post("/api/recordings/{rec_id}/transcribe")
    def transcribe_one(rec_id: int, body: dict | None = None):
        # body {"language": "es" | "auto" | "en,es" | ""}: "Transcribe in..." -- the recording keeps that speech
        # language for later runs too; "" goes back to the one in Settings
        rec = _rec(rec_id)
        if rec["status"] != "probed":
            raise app_error(409, "server.probe_first", "This recording has not been read yet. Wait until its probe job is done.")
        changed = False
        if isinstance(body, dict) and "language" in body:
            raw = str(body.get("language") or "").strip()
            if raw and not speechlang.is_known(raw):
                raise app_error(400, "server.speech.unknown_language", "Unknown speech language: {language}", language=raw)
            lang = speechlang.normalize(raw) if raw else None
            changed = (rec.get("stt_language") or None) != lang
            ctx.db.execute("UPDATE recordings SET stt_language=? WHERE id=?", (lang, rec_id))
            logger.info("[STT] speech language override rec=%s lang=%s", rec_id, lang or "settings")
        # a new language while a run is going: queue another run after it (that one still uses the old language)
        jid = _queue(rec, after_running=changed)
        return {"ok": True, "job": jid, "already_queued": jid is None,
                "language": speechlang.for_recording(ctx.cfg, ctx.db.one("SELECT * FROM recordings WHERE id=?",
                                                                          (rec_id,)))}

    @r.get("/api/stt/languages")
    def stt_languages():
        """The speech languages the pickers offer, the setting, and the model that setting really loads."""
        from core import packs
        c = ctx.cfg.stt
        eff = speechlang.effective_model(c.model, c.language)
        out = speechlang.catalog()
        out.update({"setting": c.language, "mode": speechlang.parse(c.language)["mode"],
                    "model": c.model, "effective_model": eff,
                    "effective_size_mb": float(packs.MODEL_SIZES_MB.get(eff, 0)),
                    "effective_cached": bool(packs.model_cached(eff)),
                    "redetect_windows": bool(c.redetect_windows), "translate": bool(c.translate),
                    "keywords": settings_mod.keyword_map(ctx.cfg)})
        d = speechlang.describe_msg(c.language)
        out["describe"], out["describe_i18n"] = d["message"], {"code": d["code"], "params": d["params"]}
        return out

    @r.get("/api/transcribe-all")
    def transcribe_all_plan():
        """What each scope of Transcribe all would queue: counts, hours, the time estimate."""
        out = transcripts.plan(ctx.db)
        out["stt_enabled"] = bool(ctx.cfg.stt.enabled)
        out["model"] = speechlang.effective_model(ctx.cfg.stt.model, ctx.cfg.stt.language)   # the one really used
        return out

    @r.post("/api/transcribe-all")
    def transcribe_all(body: dict | None = None, only_missing: bool = True, scope: str = ""):
        # scope: missing | outdated | all (body or query); the old ?only_missing=false still means all
        scope = ((body or {}).get("scope") or scope or ("missing" if only_missing else "all")).strip().lower()
        if scope not in transcripts.SCOPES:
            raise app_error(400, "server.bad_request", "The request was not understood.")
        if not ctx.cfg.stt.enabled:
            raise app_error(409, "server.speech.off", "Speech-to-text is off (Settings > Speech-to-text).")
        rows = transcripts.candidates(ctx.db, scope)
        n = sum(1 for rec in rows if _queue(rec) is not None)
        return {"ok": True, "scope": scope, "queued": n, "candidates": len(rows)}

    @r.get("/api/stt/status")
    def stt_status():
        from stt import whisper as stt
        s = stt.engine.status()
        s["setting_model"] = ctx.cfg.stt.model
        s["effective_model"] = speechlang.effective_model(ctx.cfg.stt.model, ctx.cfg.stt.language)
        s["setting_language"] = ctx.cfg.stt.language
        s["setting_compute"] = ctx.cfg.stt.compute
        s["enabled"] = ctx.cfg.stt.enabled
        s["fts"] = bool(ctx.db.fts)
        return s

    return r
