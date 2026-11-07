"""The stage queue: list, pause / resume, retry, skip, clear."""

from fastapi import APIRouter, HTTPException


def build(ctx) -> APIRouter:
    r = APIRouter()

    @r.get("/api/jobs")
    def jobs(state: str = "", limit: int = 200):
        return {"jobs": ctx.jobs.list(state=state, limit=max(1, min(limit, 2000))), "counts": ctx.jobs.counts()}

    @r.get("/api/jobs/counts")
    def counts():
        return ctx.jobs.counts()

    @r.post("/api/jobs/pause")
    def pause(body: dict | None = None):
        paused = True if body is None else bool(body.get("paused", True))
        ctx.jobs.set_paused(paused)
        ctx.save_settings()
        return {"ok": True, "paused": ctx.jobs.paused}

    @r.post("/api/jobs/{job_id}/retry")
    def retry(job_id: int):
        if not ctx.jobs.retry(job_id):
            raise HTTPException(409, "job is not in a retryable state")
        return {"ok": True}

    @r.post("/api/jobs/{job_id}/stop")
    def stop(job_id: int):
        """Stop a running job (a download, a proxy build); it ends as skipped."""
        if not ctx.jobs.stop_job(job_id):
            raise HTTPException(409, "that job is not running")
        return {"ok": True}

    @r.post("/api/jobs/{job_id}/skip")
    def skip(job_id: int):
        if not ctx.jobs.skip(job_id):
            raise HTTPException(409, "only queued jobs can be skipped")
        return {"ok": True}

    @r.post("/api/jobs/retry-failed")
    def retry_failed():
        n = 0
        for row in ctx.db.query("SELECT id FROM jobs WHERE state='failed'"):
            n += int(ctx.jobs.retry(row["id"]))
        return {"ok": True, "retried": n}

    @r.post("/api/jobs/clear")
    def clear(body: dict | None = None):
        # {"all": true}: the queued jobs too (running ones always finish); default: finished only
        states = ("done", "failed", "skipped", "queued") if (body or {}).get("all") else ("done", "failed", "skipped")
        return {"ok": True, "removed": ctx.jobs.clear(states)}

    @r.post("/api/jobs/reorder")
    def reorder(body: dict):
        ids = body.get("ids") or []
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            raise HTTPException(400, "ids: a list of job ids, first runs first")
        return {"ok": True, "moved": ctx.jobs.reorder(ids)}

    return r
