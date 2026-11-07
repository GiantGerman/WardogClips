"""Transcripts: read one recording's segments, search across all of them, queue
transcription, report the speech-to-text engine."""

from fastapi import APIRouter, HTTPException

from core import transcripts
from core.library import parse_name, _priority


def build(ctx) -> APIRouter:
    r = APIRouter()

    def _rec(rec_id: int) -> dict:
        rec = ctx.db.one("SELECT * FROM recordings WHERE id=?", (rec_id,))
        if rec is None:
            raise HTTPException(404, "no such recording")
        return rec

    def _queue(rec: dict) -> int | None:
        kind, name_utc = parse_name(rec["name"])
        return ctx.jobs.enqueue(rec["id"], "transcribe", priority=_priority(name_utc, rec["mtime"]))

    @r.get("/api/recordings/{rec_id}/transcript")
    def transcript(rec_id: int, words: int = 0):
        _rec(rec_id)
        return {"rec_id": rec_id, "segments": transcripts.segments(ctx.db, rec_id, with_words=bool(words)),
                "summary": transcripts.summary(ctx.db, rec_id)}

    @r.get("/api/search")
    def search(q: str = "", limit: int = 50, drive: str = ""):
        return transcripts.search(ctx.db, q, limit=limit, drive=drive)

    @r.post("/api/recordings/{rec_id}/transcribe")
    def transcribe_one(rec_id: int):
        rec = _rec(rec_id)
        if rec["status"] != "probed":
            raise HTTPException(409, "probe the recording first")
        jid = _queue(rec)
        return {"ok": True, "job": jid, "already_queued": jid is None}

    @r.get("/api/transcribe-all")
    def transcribe_all_plan():
        """What each scope of Transcribe all would queue: counts, hours, the time estimate."""
        out = transcripts.plan(ctx.db)
        out["stt_enabled"] = bool(ctx.cfg.stt.enabled)
        out["model"] = ctx.cfg.stt.model
        return out

    @r.post("/api/transcribe-all")
    def transcribe_all(body: dict | None = None, only_missing: bool = True, scope: str = ""):
        # scope: missing | outdated | all (body or query); the old ?only_missing=false still means all
        scope = ((body or {}).get("scope") or scope or ("missing" if only_missing else "all")).strip().lower()
        if scope not in transcripts.SCOPES:
            raise HTTPException(400, f"scope must be one of {', '.join(transcripts.SCOPES)}")
        if not ctx.cfg.stt.enabled:
            raise HTTPException(409, "speech-to-text is off (Settings > Speech-to-text)")
        rows = transcripts.candidates(ctx.db, scope)
        n = sum(1 for rec in rows if _queue(rec) is not None)
        return {"ok": True, "scope": scope, "queued": n, "candidates": len(rows)}

    @r.get("/api/stt/status")
    def stt_status():
        from stt import whisper as stt
        s = stt.engine.status()
        s["setting_model"] = ctx.cfg.stt.model
        s["setting_compute"] = ctx.cfg.stt.compute
        s["enabled"] = ctx.cfg.stt.enabled
        s["fts"] = bool(ctx.db.fts)
        return s

    return r
