"""Drive-aware parallelism: per-drive benchmark, slots and running jobs; benchmark on request.

GET  /api/drives                 {drives: [...], capacity: {...}}
POST /api/drives/benchmark       {drive?: "D:"} -> runs in a background thread per drive; progress
                                 arrives as bus events kind "drives" (bench_waiting / bench_started /
                                 bench_done / bench_skipped / bench_failed)
"""

from fastapi import APIRouter

from core.drives import norm_drive
from utils.i18n import app_error


def build(ctx) -> APIRouter:
    r = APIRouter()

    @r.get("/api/drives")
    def drives():
        return {"drives": ctx.jobs.drive_status(), "capacity": ctx.jobs.capacity()}

    @r.post("/api/drives/benchmark")
    def benchmark(body: dict | None = None):
        drive = norm_drive(str((body or {}).get("drive") or ""))
        mgr = ctx.jobs.drives
        if drive:
            if drive not in mgr.known_drives():
                raise app_error(404, "server.drive.no_files", "There are no library files on {drive}.", drive=drive)
            if drive in mgr.status():
                raise app_error(409, "server.drive.busy", "{drive} is already being measured.", drive=drive)
        started = mgr.request(drive or None)
        if drive and not started:
            raise app_error(409, "server.drive.busy", "{drive} is already being measured.", drive=drive)
        ctx.bus.publish("drives", action="bench_requested", drives=started)
        return {"ok": True, "started": started}

    return r
