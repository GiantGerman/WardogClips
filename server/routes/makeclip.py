"""Make clip: queue a trimmed copy of a moment into the clip pool, list the clips made, open the folder."""

import json
import logging
import time

from fastapi import APIRouter

from core.jobs import REORDER_HEADROOM
from core.messages import http_error
from media import makeclip
from utils.i18n import app_error, with_msg

logger = logging.getLogger("wclips.makeclip")


def build(ctx) -> APIRouter:
    r = APIRouter()

    @r.post("/api/makeclip")
    def make(body: dict):
        """{event_id} (its clip in / out) or {rec_id, in, out}; optional in / out override the event's, mode copy | exact."""
        if not isinstance(body, dict):
            raise app_error(400, "server.bad_request", "The request was not understood.")
        ev = None
        if body.get("event_id") is not None:
            ev = ctx.db.one("SELECT * FROM events WHERE id=?", (body["event_id"],))
            if ev is None:
                raise app_error(404, "server.no_event", "That event does not exist anymore.")
        rec_id = ev["rec_id"] if ev else body.get("rec_id")
        rec = ctx.db.one("SELECT * FROM recordings WHERE id=?", (rec_id,)) if rec_id is not None else None
        if rec is None:
            raise app_error(404, "server.no_recording", "That recording does not exist anymore.")
        if rec.get("missing"):
            raise app_error(409, "server.makeclip_missing", "The recording {name} is not in its folder anymore.",
                            name=rec.get("name") or "")
        try:
            arg = makeclip.plan(ctx.db, ctx.cfg, rec, ev, body.get("in"), body.get("out"), body.get("mode"))
        except makeclip.MakeClipError as e:
            raise http_error(400 if "probe" not in str(e) else 409, e)
        # user-started: ahead of the probe backlog in the io lane and of any hand-made queue order
        prio = int(time.time() // 60) + REORDER_HEADROOM + 1000
        jid = ctx.jobs.enqueue(rec["id"], "makeclip", priority=prio, arg=json.dumps(arg, sort_keys=True))
        logger.info("[MakeClip] queued rec=%s job=%s in=%.3f out=%.3f mode=%s", rec["id"], jid, arg["in"], arg["out"], arg["mode"])
        out = {"ok": True, "job": jid, "already_queued": jid is None, **arg, "dir": str(makeclip.pool_dir(ctx.cfg))}
        # a translatable text in a normal answer: "note" (English) + "note_i18n" {code, params} for the page
        return with_msg(out, "note", "server.makeclip_queued", "The clip is being made and will be saved in {dir}.",
                        dir=out["dir"])

    @r.get("/api/makeclip/clips")
    def clips(limit: int = 50, rec_id: int | None = None):
        return {"dir": str(makeclip.pool_dir(ctx.cfg)), "clips": makeclip.list_clips(ctx.db, limit, rec_id)}

    @r.post("/api/makeclip/open-folder")
    def open_folder(body: dict | None = None):
        """Explorer on the clip folder; {id} selects that clip."""
        path = None
        cid = (body or {}).get("id")
        if cid is not None:
            makeclip.ensure_table(ctx.db)
            row = ctx.db.one("SELECT path FROM made_clips WHERE id=?", (cid,))
            if row is None:
                raise app_error(404, "server.no_clip", "That clip does not exist anymore.")
            path = row["path"]
        try:
            makeclip.reveal(path, makeclip.pool_dir(ctx.cfg))
        except OSError as e:
            raise app_error(500, "server.explorer_failed", "Could not open File Explorer: {error}", error=str(e))
        return {"ok": True, "dir": str(makeclip.pool_dir(ctx.cfg))}

    return r
