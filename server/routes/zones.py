"""Detection zone templates (core/zones.py): the streamers list, template CRUD + dated versions,
sample frames, which template a recording uses (+ pinning), export / import and a re-detect of
one streamer's footage after their zones changed. The zones editor (web/js/zones.js) is the client.
"""

import logging
import time

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from core import zones
from core.library import rec_to_dict, vod_provider
from core.messages import http_error
from utils.i18n import app_error

logger = logging.getLogger("wclips.zones")

# a re-detect after a zones change runs before the normal backlog (epoch-minute priorities) but
# below a hand-dragged order in the Jobs tab (jobs.REORDER_HEADROOM = 100000 minutes)
REDETECT_BUMP = 1000


def _out(t: dict | None) -> dict | None:
    if t is None:
        return None
    t = dict(t)
    t["sample_url"] = (f"/api/zones/templates/{t['id']}/sample.jpg?v={int(t.get('updated_utc') or 0)}"
                       if zones.sample_path(t.get("sample_jpg")) else None)
    return t


def build(ctx) -> APIRouter:
    r = APIRouter()

    def _rec(rec_id) -> dict:
        try:
            rec = ctx.db.one("SELECT * FROM recordings WHERE id=?", (int(rec_id),))
        except (TypeError, ValueError):
            rec = None
        if rec is None:
            raise app_error(404, "server.no_recording", "That recording does not exist anymore.")
        return rec

    def _tpl(tpl_id: int) -> dict:
        t = zones.get(ctx.db, tpl_id)
        if t is None:
            raise app_error(404, "server.zones.no_template", "That zone template does not exist anymore.")
        return t

    def _changed(action: str, **kw) -> None:
        ctx.bus.publish("zones", action=action, **kw)

    def _grab(tpl_id: int, body: dict) -> str | None:
        """Grab the sample frame named by body.sample_rec_id / sample_t; -> an error text or None."""
        rec = _rec(body["sample_rec_id"])
        from vision import frames
        try:
            zones.set_sample(ctx.db, tpl_id, rec, float(body.get("sample_t") or 0))
        except (frames.FrameError, OSError) as e:
            logger.warning("[Zones] sample grab failed tpl=%s rec=%s err=%s", tpl_id, rec["id"], e)
            return str(e)[:300]
        return None

    # ---- streamers -----------------------------------------------------------------------
    @r.get("/api/zones/streamers")
    def streamers():
        rows: dict[tuple, dict] = {}
        obs = {"provider": zones.OBS, "channel_key": zones.ANY, "channel_name": zones.OBS_NAME,
               "n_recordings": 0, "n_templates": 0, "latest_rec_id": None}
        obs["n_recordings"] = ctx.db.scalar("SELECT COUNT(*) FROM recordings WHERE kind != 'vod' AND missing=0") or 0
        latest = ctx.db.one("SELECT id FROM recordings WHERE kind != 'vod' AND missing=0 AND status='probed'"
                            " ORDER BY name_utc DESC, id DESC LIMIT 1")
        obs["latest_rec_id"] = latest["id"] if latest else None
        best: dict[tuple, tuple] = {}
        for rec in ctx.db.query("SELECT id, name, channel, channel_key, name_utc, status, missing FROM recordings"
                                " WHERE kind='vod' AND channel_key IS NOT NULL AND channel_key != ''"):
            prov = vod_provider(rec["name"])
            if not prov:
                continue
            k = (zones.canon_provider(prov), zones.canon_key(rec["channel_key"]))
            row = rows.setdefault(k, {"provider": k[0], "channel_key": k[1], "channel_name": "", "n_recordings": 0,
                                      "n_templates": 0, "latest_rec_id": None})
            if not rec["missing"]:
                row["n_recordings"] += 1
            # the newest probed, present recording is the one the editor grabs frames from
            rank = (rec["status"] == "probed" and not rec["missing"], rec["name_utc"] or 0, rec["id"])
            if k not in best or rank > best[k]:
                best[k] = rank
                row["latest_rec_id"] = rec["id"]
                row["channel_name"] = rec["channel"] or row["channel_name"] or k[1]
        for t in ctx.db.query("SELECT provider, channel_key, MAX(channel_name) channel_name, COUNT(*) n"
                              " FROM zone_templates GROUP BY provider, channel_key"):
            k = (t["provider"], t["channel_key"])
            if k == (zones.OBS, zones.ANY):
                obs["n_templates"] = t["n"]
                continue
            name = t["channel_name"] or (f"All {t['provider']} VODs" if t["channel_key"] == zones.ANY else t["channel_key"])
            row = rows.setdefault(k, {"provider": k[0], "channel_key": k[1], "channel_name": name, "n_recordings": 0,
                                      "n_templates": 0, "latest_rec_id": None})
            row["n_templates"] = t["n"]
            if not row["channel_name"]:
                row["channel_name"] = name
        rest = sorted(rows.values(), key=lambda x: (x["provider"].lower(), x["channel_key"] != zones.ANY,
                                                   (x["channel_name"] or x["channel_key"]).lower()))
        return [obs, *rest]

    # ---- templates -----------------------------------------------------------------------
    @r.get("/api/zones/templates")
    def list_templates(provider: str = "", channel_key: str = ""):
        return [_out(t) for t in zones.list_templates(ctx.db, provider or None, channel_key or None)]

    @r.get("/api/zones/templates/{tpl_id}")
    def one_template(tpl_id: int):
        return _out(_tpl(tpl_id))

    @r.post("/api/zones/templates")
    def create_template(body: dict):
        if body.get("sample_rec_id") not in (None, ""):
            _rec(body["sample_rec_id"])                      # 404 before anything is written
        try:
            t = zones.create(ctx.db, body.get("provider") or "", body.get("channel_key") or "", body.get("zones"),
                             channel_name=body.get("channel_name") or "", name=body.get("name") or "",
                             effective_from=body.get("effective_from") or 0, note=body.get("note") or "",
                             sample_w=body.get("sample_w"), sample_h=body.get("sample_h"))
        except ValueError as e:
            raise http_error(400, e)
        err = _grab(t["id"], body) if body.get("sample_rec_id") not in (None, "") else None
        _changed("created", id=t["id"], provider=t["provider"], channel_key=t["channel_key"])
        out = _out(zones.get(ctx.db, t["id"]))
        if err:
            out["sample_error"] = err
        return out

    @r.put("/api/zones/templates/{tpl_id}")
    def update_template(tpl_id: int, body: dict):
        _tpl(tpl_id)
        fields = {k: body[k] for k in ("name", "zones", "effective_from", "note", "channel_name") if k in body}
        grab = body.get("sample_rec_id") not in (None, "")
        if grab:
            _rec(body["sample_rec_id"])
        try:
            t = zones.update(ctx.db, tpl_id, **fields) if fields else _tpl(tpl_id)
        except ValueError as e:
            raise http_error(400, e)
        err = _grab(tpl_id, body) if grab else None
        _changed("updated", id=tpl_id, provider=t["provider"], channel_key=t["channel_key"])
        out = _out(zones.get(ctx.db, tpl_id))
        if err:
            out["sample_error"] = err
        return out

    @r.delete("/api/zones/templates/{tpl_id}")
    def delete_template(tpl_id: int):
        t = _tpl(tpl_id)
        n = zones.delete(ctx.db, tpl_id)
        _changed("deleted", id=tpl_id, provider=t["provider"], channel_key=t["channel_key"])
        return {"ok": True, "unpinned": n}

    @r.post("/api/zones/templates/{tpl_id}/version")
    def new_version(tpl_id: int, body: dict):
        _tpl(tpl_id)
        if "effective_from" not in body:
            raise app_error(400, "server.zones.date_required", "Pick the date this version starts from.")
        grab = body.get("sample_rec_id") not in (None, "")
        if grab:
            _rec(body["sample_rec_id"])
        over = {k: body[k] for k in ("name", "zones", "note") if body.get(k) is not None}
        try:
            t = zones.new_version(ctx.db, tpl_id, body.get("effective_from") or 0, **over)
        except ValueError as e:
            raise http_error(400, e)
        err = _grab(t["id"], body) if grab else None
        _changed("created", id=t["id"], provider=t["provider"], channel_key=t["channel_key"])
        out = _out(zones.get(ctx.db, t["id"]))
        if err:
            out["sample_error"] = err
        return out

    @r.get("/api/zones/templates/{tpl_id}/sample.jpg")
    def sample(tpl_id: int):
        p = zones.sample_path(_tpl(tpl_id).get("sample_jpg"))
        if p is None:
            raise app_error(404, "server.zones.no_sample", "This template has no sample frame.")
        return FileResponse(str(p), media_type="image/jpeg", headers={"Cache-Control": "no-cache"})

    @r.get("/api/zones/default")
    def default(width: int = 3440, height: int = 1440):
        if not (16 <= width <= 16384 and 16 <= height <= 16384):
            raise app_error(400, "server.zones.bad_size", "The width or height is out of range.")
        z = zones.default_zones(width, height)
        return {"zones": z, "width": width, "height": height,
                "geometry": zones.geometry_json(zones.geometry(z, width, height))}

    # ---- recordings ----------------------------------------------------------------------
    @r.get("/api/zones/for-recording/{rec_id}")
    def for_recording(rec_id: int):
        rec = _rec(rec_id)
        from vision import hud
        width, height = int(rec.get("width") or hud.BASE_W), int(rec.get("height") or hud.BASE_H)
        tpl, source = zones.resolve(ctx.db, rec)
        provider, key = zones.streamer_of(rec)
        name = zones.OBS_NAME if provider == zones.OBS else (rec.get("channel") or key or "")
        z = tpl["zones"] if tpl else zones.default_zones(width, height)
        try:
            geo = zones.geometry_json(zones.geometry(z, width, height))
        except ValueError:
            geo = None
        return {"source": source, "template": _out(tpl), "zones": z, "width": width, "height": height,
                "provider": provider, "channel_key": key, "channel_name": name,
                "pinned_id": rec.get("zone_template_id"), "geometry": geo}

    @r.post("/api/recordings/{rec_id}/zone-template")
    def pin(rec_id: int, body: dict):
        _rec(rec_id)
        tid = body.get("template_id")
        try:
            zones.pin(ctx.db, rec_id, None if tid in (None, "") else int(tid))
        except (TypeError, ValueError):
            raise app_error(400, "server.bad_request", "The request was not understood.")
        except zones.NotFound as e:
            raise http_error(404, e)
        fresh = ctx.db.one("SELECT * FROM recordings WHERE id=?", (rec_id,))
        ctx.bus.publish("recording", action="updated", rec=rec_to_dict(fresh))
        return {"ok": True, "rec_id": rec_id, "template_id": fresh["zone_template_id"]}

    # ---- export / import -----------------------------------------------------------------
    @r.get("/api/zones/export")
    def export(ids: str = "", samples: int = 1):
        try:
            id_list = [int(x) for x in ids.split(",") if x.strip()] if ids else None
        except ValueError:
            raise app_error(400, "server.bad_request", "The request was not understood.")
        data = zones.export(ctx.db, id_list, include_samples=bool(samples))
        fname = f"wardogclips-zones-{time.strftime('%Y-%m-%d')}.json"
        return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    @r.post("/api/zones/import")
    def import_(body: dict):
        if "data" not in body:
            raise app_error(400, "server.zones.import_empty", "Choose a zone template file to import.")
        try:
            res = zones.import_(ctx.db, body["data"], overwrite=bool(body.get("overwrite")))
        except ValueError as e:
            raise http_error(400, e)
        if res["added"] or res["updated"]:
            _changed("imported", added=res["added"], updated=res["updated"])
        return res

    # ---- re-detect -----------------------------------------------------------------------
    @r.post("/api/zones/redetect")
    def redetect(body: dict):
        provider = zones.canon_provider(body.get("provider") or "")
        key = zones.canon_key(body.get("channel_key") or "")
        if not provider or not key:
            raise app_error(400, "server.zones.streamer_required", "Pick a site and a streamer.")
        recs = ctx.db.query("SELECT * FROM recordings WHERE status='probed' AND missing=0")
        mine = []
        for rec in recs:
            p, k = zones.streamer_of(rec)
            # (provider, '*') = every recording of that site (for OBS: all local OBS footage)
            if p == provider and (k == key or key == zones.ANY):
                mine.append(rec)
        mine.sort(key=lambda x: zones.rec_start(x) or 0, reverse=True)      # newest first
        base = int(time.time() // 60) + REDETECT_BUMP
        # a detect that is RUNNING resolved the zones before this change: it gets a fresh run queued
        # behind it (enqueue after_running; the claim waits for the running one). A QUEUED detect
        # resolves the template when it starts, so it only moves up.
        running = {x["rec_id"] for x in ctx.db.query("SELECT DISTINCT rec_id FROM jobs WHERE stage='detect'"
                                                       " AND state='running' AND arg IS NULL")}
        queued = after_current = bumped = 0
        for i, rec in enumerate(mine):
            pri = base + len(mine) - i
            if ctx.jobs.enqueue(rec["id"], "detect", priority=pri, after_running=True) is not None:
                queued += 1
                if rec["id"] in running:
                    after_current += 1
            else:
                bumped += ctx.db.execute("UPDATE jobs SET priority=MAX(priority, ?) WHERE rec_id=? AND stage='detect'"
                                         " AND state='queued'", (pri, rec["id"]))
        logger.info("[Zones] redetect provider=%s key=%s recordings=%d queued=%d after_current=%d bumped=%d", provider,
                    key, len(mine), queued, after_current, bumped)
        # queued = new detect jobs (restarting_after_current of them wait for a run already going)
        return {"ok": True, "queued": queued, "restarting_after_current": after_current,
                "already_queued": len(mine) - queued, "candidates": len(mine)}

    return r
