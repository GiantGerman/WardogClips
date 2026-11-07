"""Library + recordings: scan the roots, list / inspect recordings, re-probe, reveal in Explorer."""

import logging
import os
import subprocess

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger("wclips.library")


def build(ctx) -> APIRouter:
    r = APIRouter()

    @r.get("/api/library/summary")
    def summary():
        return ctx.library.summary()

    @r.post("/api/library/scan")
    async def scan():
        return await run_in_threadpool(ctx.library.scan)

    @r.get("/api/recordings")
    def recordings(drive: str = "", q: str = "", status: str = "", limit: int = 500):
        return {"recordings": ctx.library.list(drive=drive, q=q, status=status, limit=limit)}

    @r.get("/api/recordings/{rec_id}")
    def recording(rec_id: int):
        rec = ctx.library.get(rec_id)
        if rec is None:
            raise HTTPException(404, "no such recording")
        return rec

    @r.post("/api/recordings/{rec_id}/probe")
    def reprobe(rec_id: int):
        rec = ctx.library.get(rec_id)
        if rec is None:
            raise HTTPException(404, "no such recording")
        ctx.db.execute("UPDATE recordings SET status='new', error=NULL WHERE id=?", (rec_id,))
        jid = ctx.jobs.enqueue(rec_id, "probe", priority=5)
        return {"ok": True, "job": jid}

    @r.post("/api/recordings/{rec_id}/reveal")
    def reveal(rec_id: int):
        """Open Explorer with the file selected (local tool; the footage is never modified)."""
        rec = ctx.library.get(rec_id)
        if rec is None:
            raise HTTPException(404, "no such recording")
        if not os.path.exists(rec["path"]):
            raise HTTPException(404, "file is missing")
        try:
            subprocess.Popen(["explorer.exe", "/select,", rec["path"]])
        except OSError as e:
            raise HTTPException(500, str(e))
        return {"ok": True}

    @r.post("/api/recordings/probe-all")
    def probe_all(only_new: bool = True):
        where = "WHERE status IN ('new','error') AND missing=0" if only_new else "WHERE missing=0"
        rows = ctx.db.query(f"SELECT id FROM recordings {where} ORDER BY COALESCE(name_utc, mtime) DESC")
        n = 0
        for row in rows:
            ctx.db.execute("UPDATE recordings SET status=CASE WHEN status='error' THEN 'new' ELSE status END WHERE id=?",
                           (row["id"],))
            if ctx.jobs.enqueue(row["id"], "probe") is not None:
                n += 1
        return {"ok": True, "queued": n, "candidates": len(rows)}

    return r
