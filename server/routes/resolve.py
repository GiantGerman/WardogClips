"""DaVinci Resolve hand-off (markers, selects timeline) and the CSV / EDL export.

Selection body shared by every POST (see resolve/export.py):
    {rec_ids?, event_ids?, status?: kept|kept,new|any, kinds?, min_score?, order?: score|time}
Resolve errors -> 503 with the reason; a bad / empty selection -> 400.
"""

import logging

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse

from resolve import client as rc
from resolve import export

logger = logging.getLogger("wclips.resolve")


def build(ctx) -> APIRouter:
    r = APIRouter()

    def _select(body: dict | None) -> list[dict]:
        try:
            events = export.select(ctx.db, ctx.cfg, body or {})
        except export.SelectionError as e:
            raise HTTPException(400, str(e))
        if not events:
            raise HTTPException(400, "no events match the selection (status false is never sent; default status is kept)")
        return events

    def _enabled() -> None:
        if not ctx.cfg.resolve.enabled:
            raise HTTPException(409, "the Resolve hand-off is disabled in Settings > Resolve")

    def _touched(events: list[dict]) -> None:
        for rec_id in sorted({e["rec_id"] for e in events}):
            ctx.bus.publish("events", action="updated", rec_id=rec_id)

    link = ctx.resolve_link

    @r.get("/api/resolve/status")
    async def resolve_status(fresh: int = 0):
        """Cheap to poll: the connection probe is cached a few seconds (fresh=1 forces one)."""
        s = await run_in_threadpool(rc.status, bool(fresh))
        link.seen_status(s)
        s["enabled"] = bool(ctx.cfg.resolve.enabled)
        s["bin"] = ctx.cfg.resolve.bin
        s["link"] = link.snapshot()
        s["has_project"] = bool(s.get("connected")) and link.accepts(s.get("project"))
        return s

    @r.post("/api/resolve/start")
    def resolve_start():
        """Start Resolve (unless it is up) and wait for a project; the page follows the SSE 'resolve' event."""
        _enabled()
        try:
            return {"ok": True, "link": link.start_now()}
        except rc.ResolveError as e:
            raise HTTPException(409, str(e))

    @r.post("/api/resolve/cancel")
    def resolve_cancel(body: dict | None = None):
        """Drop waiting hand-offs: {id} one, {} all (and stop waiting for Resolve)."""
        op_id = (body or {}).get("id")
        return {"ok": True, "cancelled": link.cancel(int(op_id) if op_id is not None else None), "link": link.snapshot()}

    @r.post("/api/resolve/use-open-project")
    async def resolve_use_open():
        """The project open in Resolve right now is the one, even an 'Untitled Project' (until the waiting
        hand-offs ran or Resolve closes)."""
        st = await run_in_threadpool(rc.status, True)
        if not st.get("connected") or not st.get("project"):
            raise HTTPException(409, "Resolve has no project open")
        return {"ok": True, "link": link.use_open_project(st.get("project"))}

    async def _handoff(kind: str, label: str, fn):
        """Run now when Resolve has a project; otherwise (Settings > Resolve > launch_on_send) wait for
        it -- Resolve is started if it is down -- and run then: HTTP 202 {queued, op}, the result
        arrives on the SSE 'resolve' event. The module missing is always 503."""
        try:
            await run_in_threadpool(rc.load_module)
        except rc.ResolveUnavailable as e:
            raise HTTPException(503, str(e))
        snap = link.snapshot()
        wait = ctx.cfg.resolve.launch_on_send
        # the link's state only means something while its watcher runs (an idle link's last state is history)
        if wait and (snap["waiting"] or (snap["watching"] and snap["state"] in ("starting", "no_project"))):
            return _queued(kind, label, fn)          # keep the order behind what already waits
        try:
            st = await run_in_threadpool(rc.status)
            link.seen_status(st)
            if not st.get("connected"):
                if wait:
                    # the watcher probes fresh before it launches anything: no extra ~4 s child probe here
                    return _queued(kind, label, fn)
                st = await run_in_threadpool(rc.status, True)     # don't trust a cached "down" (45 s)
        except Exception as e:                       # noqa: BLE001
            raise HTTPException(503, str(e))
        if not st.get("available"):
            raise HTTPException(503, st.get("error") or rc.NOT_RUNNING)
        ok_project = link.accepts(st.get("project"))
        if st.get("connected") and ok_project:
            try:
                return await run_in_threadpool(fn)
            except rc.ResolveNotRunning as e:
                if not wait:
                    raise HTTPException(503, str(e))
            except rc.ResolveError as e:
                logger.warning("[Resolve] %s failed error=%s", kind, e)
                raise HTTPException(503, str(e))
        elif not wait:
            raise HTTPException(503, (st.get("error") or rc.NOT_RUNNING) if not st.get("connected")
                                else "no project open in Resolve -- open the project you are cutting first")
        return _queued(kind, label, fn)

    def _queued(kind, label, fn):
        op = link.submit(kind, label, fn)
        return JSONResponse(status_code=202, content={"queued": True, "op": op, "link": link.snapshot()})

    @r.post("/api/resolve/preview")
    def preview(body: dict | None = None):
        """What a hand-off would send, without Resolve: counts + the merged clips."""
        try:
            events = export.select(ctx.db, ctx.cfg, body or {})
        except export.SelectionError as e:
            raise HTTPException(400, str(e))
        order = str((body or {}).get("order") or "score").lower()
        clips = export.merge_clips(events, order)
        return {"events": len(events), "clips": len(clips), "recordings": len({e["rec_id"] for e in events}),
                "seconds": round(sum(c["clip_out_s"] - c["clip_in_s"] for c in clips), 1),
                "items": [{"event_ids": c["event_ids"], "rec_id": c["rec_id"], "rec_name": c["rec_name"],
                           "clip_in_s": c["clip_in_s"], "clip_out_s": c["clip_out_s"], "score": c["score"],
                           "kind": c["kind"], "name": " + ".join(export.marker_name(x) for x in c["events"])}
                          for c in clips]}

    def _fresh(body: dict | None) -> list[dict]:
        """The selection again when the hand-off actually runs: a hand-off that waited for Resolve (up to
        wait_min) must not send events marked false or with in / out changed in the meantime."""
        try:
            return export.select(ctx.db, ctx.cfg, body or {})
        except export.SelectionError:
            return []

    @r.post("/api/resolve/markers")
    async def markers(body: dict | None = None):
        _enabled()
        events = _select(body)

        def run():
            evs = _fresh(body)
            if not evs:
                return {"ok": True, "project": None, "sent": 0, "markers": [], "failed": [], "imported": [],
                        "note": "nothing left to send (the events changed while waiting)"}
            res = rc.send_markers(ctx.db, ctx.cfg, evs)
            _touched(evs)
            return res
        return await _handoff("markers", f"{len(events)} marker{'s' if len(events) != 1 else ''}", run)

    @r.post("/api/resolve/selects")
    async def selects(body: dict | None = None):
        _enabled()
        events = _select(body)
        b = body or {}
        order = str(b.get("order") or "score").lower()
        name = str(b.get("name") or "").strip() or None

        def run():
            evs = _fresh(body)
            if not evs:
                return {"ok": True, "project": None, "timeline": None, "clips": 0, "events": 0, "placed": [],
                        "failed": [], "imported": [], "note": "nothing left to send (the events changed while waiting)"}
            return rc.build_selects(ctx.db, ctx.cfg, evs, order, name)
        return await _handoff("selects", f"selects timeline from {len(events)} event{'s' if len(events) != 1 else ''}", run)

    @r.post("/api/export")
    async def export_files(body: dict | None = None):
        b = body or {}
        fmt = str(b.get("format") or "both").lower()
        try:
            res = await run_in_threadpool(export.export, ctx.db, ctx.cfg, b, fmt)
        except export.SelectionError as e:
            raise HTTPException(400, str(e))
        for f in res["files"]:
            f["url"] = f"/api/export/files/{f['name']}"
        return res

    @r.get("/api/export/files/{name}")
    def export_file(name: str):
        p = export.safe_out_file(name)
        if p is None:
            raise HTTPException(404, "no such export")
        media = "text/csv" if p.suffix.lower() == ".csv" else "text/plain"
        return FileResponse(p, media_type=media, filename=p.name)

    return r
