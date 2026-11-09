"""Events (kills / deaths / ...), detection jobs, evidence frames and the calibration data."""

import json
import re
import time

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse

from core.library import _priority, parse_name
from core.messages import http_error
from utils.i18n import app_error
from utils import paths

KINDS = ("kill", "assist", "death", "spotted", "revive", "squad", "voice", "manual")


def _ev(row: dict) -> dict:
    out = dict(row)
    try:
        out["source"] = json.loads(out.get("source") or "{}")
    except json.JSONDecodeError:
        out["source"] = {}
    return out


def build(ctx) -> APIRouter:
    r = APIRouter()

    def _rec(rec_id: int) -> dict:
        rec = ctx.db.one("SELECT * FROM recordings WHERE id=?", (rec_id,))
        if rec is None:
            raise app_error(404, "server.no_recording", "That recording does not exist anymore.")
        return rec

    def _queue(rec: dict, stage: str = "detect", bump: int = 0) -> int | None:
        kind, name_utc = parse_name(rec["name"])
        return ctx.jobs.enqueue(rec["id"], stage, priority=_priority(name_utc, rec["mtime"]) + bump)

    @r.get("/api/recordings/{rec_id}/events")
    def rec_events(rec_id: int, include_false: int = 0):
        _rec(rec_id)
        where = "" if include_false else " AND status != 'false'"
        rows = ctx.db.query(f"SELECT * FROM events WHERE rec_id=?{where} ORDER BY t_s", (rec_id,))
        return {"rec_id": rec_id, "events": [_ev(x) for x in rows]}

    @r.get("/api/events")
    def all_events(kind: str = "", status: str = "", drive: str = "", min_score: float = 0.0, limit: int = 300):
        where, params = ["1=1"], []
        if kind:
            kinds = [k for k in kind.split(",") if k in KINDS]
            if kinds:
                where.append("e.kind IN (%s)" % ",".join("?" * len(kinds)))
                params += kinds
        if status:
            where.append("e.status=?")
            params.append(status)
        else:
            where.append("e.status != 'false'")
        if drive:
            where.append("r.drive=?")
            params.append(drive.upper())
        if min_score:
            where.append("e.score >= ?")
            params.append(float(min_score))
        params.append(max(1, min(int(limit), 2000)))
        rows = ctx.db.query("SELECT e.*, r.name rec_name, r.drive, r.kind rec_kind, r.name_utc, r.duration_s FROM events e"
                            " JOIN recordings r ON r.id=e.rec_id WHERE " + " AND ".join(where) +
                            " ORDER BY e.score DESC, e.t_s LIMIT ?", params)
        return {"events": [_ev(x) for x in rows]}

    @r.get("/api/find-clips")
    def find_clips(request: Request):
        """The left panel's clip search: kinds, status, min_score, player, min_dist, streak, said +
        said_window, from / to (epoch s), rec_kind, drive, order, limit (core/clipsearch.py)."""
        from core import clipsearch
        try:
            out = clipsearch.search(ctx.db, dict(request.query_params), ctx.cfg.highlights.multikill_s)
        except ValueError as e:
            raise http_error(400, e)
        out["events"] = [_ev(x) for x in out["events"]]
        return out

    @r.get("/api/events/{event_id}")
    def one_event(event_id: int):
        row = ctx.db.one("SELECT e.*, r.name rec_name FROM events e JOIN recordings r ON r.id=e.rec_id WHERE e.id=?", (event_id,))
        if row is None:
            raise app_error(404, "server.no_event", "That event does not exist anymore.")
        return _ev(row)

    @r.post("/api/events/{event_id}")
    def update_event(event_id: int, body: dict):
        row = ctx.db.one("SELECT id, status FROM events WHERE id=?", (event_id,))
        if row is None:
            raise app_error(404, "server.no_event", "That event does not exist anymore.")
        sets, params = [], []
        if "status" in body:
            if body["status"] not in ("new", "kept", "false"):
                raise app_error(400, "server.event.bad_status", "The status must be new, kept or false.")
            sets.append("status=?")
            params.append(body["status"])
        if "note" in body:
            sets.append("note=?")
            params.append(str(body["note"])[:500])
        if "t_s" in body:
            sets.append("t_s=?")
            params.append(float(body["t_s"]))
        if "t_end_s" in body:
            sets.append("t_end_s=?")
            params.append(None if body["t_end_s"] is None else float(body["t_end_s"]))
        if not sets:
            raise app_error(400, "server.event.nothing_to_change", "There is nothing to change.")
        params.append(event_id)
        ctx.db.execute(f"UPDATE events SET {', '.join(sets)} WHERE id=?", params)
        rescored = 0
        if "status" in body and body["status"] != row["status"]:
            # the verdict re-weights the 'new' events that look the same (core/learning.py)
            from core import learning
            try:
                rescored = learning.on_status_change(ctx.db, event_id)
            except Exception as e:  # noqa: BLE001 - learning must never block a verdict
                import logging
                logging.getLogger("wclips.learning").warning("[Learning] rescore failed event=%s err=%s", event_id, e)
        ev = ctx.db.one("SELECT * FROM events WHERE id=?", (event_id,))
        ctx.bus.publish("events", action="updated", rec_id=ev["rec_id"], event_id=event_id)
        if rescored:
            ctx.bus.publish("events", action="rescored", n=rescored)
        return {**_ev(ev), "rescored": rescored}

    @r.post("/api/recordings/{rec_id}/events")
    def add_manual(rec_id: int, body: dict):
        _rec(rec_id)
        try:
            t = float(body.get("t_s"))
        except (TypeError, ValueError):
            raise app_error(400, "server.event.time_required", "The event needs a time in seconds.")
        note = str(body.get("note") or "")[:500]
        eid = ctx.db.execute(
            "INSERT INTO events (rec_id, t_s, kind, subkind, score, confidence, source, status, note, created_utc)"
            " VALUES (?,?,'manual','',1.0,1.0,?, 'kept', ?, ?)", (rec_id, t, json.dumps({"manual": True}), note, time.time()))
        ctx.bus.publish("events", action="updated", rec_id=rec_id, event_id=eid)
        return _ev(ctx.db.one("SELECT * FROM events WHERE id=?", (eid,)))

    @r.delete("/api/events/{event_id}")
    def delete_event(event_id: int):
        row = ctx.db.one("SELECT rec_id, kind FROM events WHERE id=?", (event_id,))
        if row is None:
            raise app_error(404, "server.no_event", "That event does not exist anymore.")
        if row["kind"] != "manual":
            raise app_error(409, "server.event.only_manual_delete", "Only events you added yourself can be deleted. Mark a detected event as false instead.")
        ctx.db.execute("DELETE FROM events WHERE id=?", (event_id,))
        ctx.bus.publish("events", action="updated", rec_id=row["rec_id"], event_id=event_id)
        return {"ok": True}

    @r.post("/api/recordings/{rec_id}/detect")
    def detect_one(rec_id: int):
        rec = _rec(rec_id)
        if rec["status"] != "probed":
            raise app_error(409, "server.probe_first", "This recording has not been read yet. Wait until its probe job is done.")
        jid = _queue(rec, bump=10)
        return {"ok": True, "job": jid, "already_queued": jid is None}

    @r.post("/api/detect-all")
    def detect_all(only_missing: bool = True):
        if only_missing:
            rows = ctx.db.query("SELECT * FROM recordings WHERE status='probed' AND missing=0"
                                " AND json_extract(stages, '$.detect.state') IS NULL")
        else:
            rows = ctx.db.query("SELECT * FROM recordings WHERE status='probed' AND missing=0")
        n = sum(1 for rec in rows if _queue(rec) is not None)
        return {"ok": True, "queued": n, "candidates": len(rows)}

    @r.get("/api/recordings/{rec_id}/frame")
    def frame(rec_id: int, t: float = 0.0, w: int = 1280):
        rec = _rec(rec_id)
        from vision import frames
        try:
            data = frames.grab_frame(rec["path"], max(0.0, t), max_w=max(320, min(int(w), 3440)))
        except frames.FrameError as e:
            raise app_error(500, "server.frame_failed", "Could not read a frame of the recording: {error}", error=str(e))
        return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @r.get("/api/recordings/{rec_id}/thumb")
    def thumb(rec_id: int):
        """The Library row's thumbnail (media/thumbs.py: cached; the page's ?v= changes with the file)."""
        rec = _rec(rec_id)
        from media import thumbs
        f = thumbs.get(rec)
        if f is None:
            raise app_error(404, "server.no_thumbnail", "This recording has no thumbnail yet.")
        return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})

    # ---- calibration ------------------------------------------------------------------
    @r.get("/api/calibration")
    def calibration(rec_id: int = 0):
        from vision import detect as detect_mod
        from vision import hud
        rec = _rec(rec_id) if rec_id else None
        width = int(rec["width"]) if rec and rec.get("width") else hud.BASE_W
        height = int(rec["height"]) if rec and rec.get("height") else hud.BASE_H
        box = lambda v: {"x": v.x, "y": v.y, "w": v.w, "h": v.h}   # noqa: E731
        # the boxes detect really reads: a zone template (pinned / the streamer's version / provider-wide)
        # scaled to this frame, exactly as vision.detect resolves it; else the built-in HUD profile
        tpl, zsource, geo = detect_mod._zone_geometry(ctx.db, rec, width, height) if rec else (None, "default", None)
        if tpl is not None:
            profile = f"tpl:{tpl['id']}"
            rois = {k: box(v) for k, v in geo["rois"].items()}
            money_y, row_h, center = geo["money_y"], geo["feed_row_h"], box(geo["center"])
        else:
            profile = hud.profile_for(width, height, rec.get("name_utc") if rec else None, ctx.cfg.detection.hud_profile)
            rois = {k: box(v) for k, v in hud.rois(width, height, profile).items()}
            money_y, row_h = hud.money_y(height, profile), hud.feed_row_h(height, profile)
            center = box(hud.respawn_roi(width, height))
        return {"rec_id": rec_id, "width": width, "height": height, "profile": profile, "rois": rois,
                "money_y": money_y, "feed_row_h": row_h, "center": center,
                "template_id": tpl["id"] if tpl else None, "template_name": (tpl.get("name") or "") if tpl else "",
                "zones_source": zsource,
                "ticker": detect_mod.ticker_summary(ctx.db, rec_id or None),
                "names": detect_mod.names_summary(ctx.db, rec_id or None),
                "labels": ctx.db.query("SELECT * FROM ticker_labels ORDER BY id"),
                "aliases": ctx.cfg.player.aliases, "squad": ctx.cfg.player.squad,
                "known_labels": __import__("vision.ticker", fromlist=["KNOWN_LABELS"]).KNOWN_LABELS}

    def _relabel() -> int:
        """Re-run the ticker rules over every stored line (the calibration page shows the result
        at once; the next detect run uses them for events)."""
        from vision import ticker as ticker_mod
        rules = [(x["pattern"], x["label"]) for x in ctx.db.query("SELECT pattern, label FROM ticker_labels ORDER BY id")]
        n = 0
        for row in ctx.db.query("SELECT id, text, label FROM ticker_lines"):
            label, _c, _x = ticker_mod.parse_line(row["text"], rules)
            if label != row["label"]:
                ctx.db.execute("UPDATE ticker_lines SET label=? WHERE id=?", (label, row["id"]))
                n += 1
        return n

    @r.post("/api/calibration/labels")
    def add_label(body: dict):
        """{pattern: regex} or {text: "<pattern with # for numbers>"} + label."""
        label = str(body.get("label") or "").strip()
        pattern = str(body.get("pattern") or "").strip()
        text = str(body.get("text") or "").strip()
        if text and not pattern:
            pattern = re.escape(text).replace(r"\#", "#").replace("#", r"[\d,.$+]+").replace("\\ ", r"\s*")
        if not pattern or not label:
            raise app_error(400, "server.calibration.label_required", "Enter a pattern (or the text) and a label.")
        try:
            re.compile(pattern)
        except re.error:
            pattern = re.escape(pattern)
        ctx.db.execute("INSERT INTO ticker_labels (pattern, label, note, created_utc) VALUES (?,?,?,?)"
                       " ON CONFLICT(pattern) DO UPDATE SET label=excluded.label",
                       (pattern, label, str(body.get("note") or "")[:200], time.time()))
        n = _relabel()
        return {"ok": True, "labels": ctx.db.query("SELECT * FROM ticker_labels ORDER BY id"), "relabelled": n}

    @r.delete("/api/calibration/labels/{label_id}")
    def del_label(label_id: int):
        ctx.db.execute("DELETE FROM ticker_labels WHERE id=?", (label_id,))
        n = _relabel()
        return {"ok": True, "labels": ctx.db.query("SELECT * FROM ticker_labels ORDER BY id"), "relabelled": n}

    @r.post("/api/calibration/alias")
    def add_alias(body: dict):
        name = str(body.get("name") or "").strip()
        if not name:
            raise app_error(400, "server.calibration.name_required", "Enter a name.")
        kind = body.get("as", "player")
        target = ctx.cfg.player.squad if kind == "squad" else ctx.cfg.player.aliases
        if name not in target:
            target.append(name)
            ctx.save_settings()
        return {"ok": True, "aliases": ctx.cfg.player.aliases, "squad": ctx.cfg.player.squad}

    return r
