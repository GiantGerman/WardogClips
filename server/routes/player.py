"""Player: event clips (rendered on demand, cached), clip in / out, whole-recording
proxies, and the files behind them. The page plays clips with a plain <video>; FileResponse
answers Range requests, so seeking works."""

import logging
import threading
import time

from fastapi import APIRouter
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from core.library import _priority, parse_name
from utils import paths
from utils.i18n import app_error

logger = logging.getLogger("wclips.player")


def build(ctx) -> APIRouter:
    from media import clips

    r = APIRouter()
    prefetching: set[str] = set()
    pf_lock = threading.Lock()

    def _rec(rec_id: int) -> dict:
        rec = ctx.db.one("SELECT * FROM recordings WHERE id=?", (rec_id,))
        if rec is None:
            raise app_error(404, "server.no_recording", "That recording does not exist anymore.")
        if rec["status"] != "probed" or not rec.get("duration_s"):
            raise app_error(409, "server.probe_first", "This recording has not been read yet. Wait until its probe job is done.")
        return rec

    def _event(event_id: int) -> dict:
        ev = ctx.db.one("SELECT * FROM events WHERE id=?", (event_id,))
        if ev is None:
            raise app_error(404, "server.no_event", "That event does not exist anymore.")
        return ev

    async def _render(rec: dict, t0: float, t1: float, h: int) -> dict:
        try:
            out = await run_in_threadpool(clips.render_clip, ctx.db, ctx.cfg, rec, t0, t1, h)
        except clips.ClipError as e:
            raise app_error(503, "server.player.clip_failed", "The clip could not be made: {error}", error=str(e))
        out.pop("path", None)
        out["url"] = f"/api/clips/{out['name']}"
        return out

    def _prefetch(rec: dict, windows: list[tuple[float, float]], h: int) -> None:
        """Render the neighboring events' clips in the background so stepping is instant."""
        def work():
            tracks = clips.audio_plan(ctx.db, rec, ctx.cfg)
            for w0, w1 in windows:
                name = clips.clip_name(rec["id"], w0, min(w1, float(rec["duration_s"])), h, tracks)
                with pf_lock:
                    if name in prefetching:
                        continue
                    prefetching.add(name)
                try:
                    clips.render_clip(ctx.db, ctx.cfg, rec, w0, w1, h)
                except Exception as e:  # noqa: BLE001 - prefetch is best effort
                    logger.debug("[Player] prefetch failed %s: %s", name, e)
                finally:
                    with pf_lock:
                        prefetching.discard(name)
        threading.Thread(target=work, name="clip-prefetch", daemon=True).start()

    @r.get("/api/recordings/{rec_id}/clip")
    async def rec_clip(rec_id: int, t0: float, t1: float, h: int = 720):
        """Any window of a recording (the player's 'look around here')."""
        rec = _rec(rec_id)
        if t1 <= t0:
            raise app_error(400, "server.player.bad_window", "The end time must be after the start time.")
        out = await _render(rec, t0, t1, h)
        return out

    @r.get("/api/events/{event_id}/clip")
    async def event_clip(event_id: int, h: int = 720, handles: float = 5.0, prefetch: int = 2):
        """The event's clip: in / out (user-set or pre / post roll) plus handles either side."""
        ev = _event(event_id)
        rec = _rec(ev["rec_id"])
        w = clips.clip_window(ev, rec, ctx.cfg, handles_s=max(0.0, min(handles, 30.0)))
        out = await _render(rec, w["w0"], w["w1"], h)
        out.update({"event_id": event_id, "rec_id": rec["id"], "t": w["t"], "in": w["in"], "out": w["out"],
                    "user_bounds": ev.get("clip_in") is not None or ev.get("clip_out") is not None})
        if prefetch > 0:
            # neighbors by time (the event itself may be a false positive, which this list leaves out)
            evs = ctx.db.query("SELECT * FROM events WHERE rec_id=? AND status != 'false' AND id != ? ORDER BY t_s",
                               (rec["id"], event_id))
            after = [x for x in evs if x["t_s"] >= ev["t_s"]]
            before = [x for x in evs if x["t_s"] < ev["t_s"]]
            near = after[:prefetch] + before[-1:]
            wins = []
            for x in near:
                xw = clips.clip_window(x, rec, ctx.cfg, handles_s=max(0.0, min(handles, 30.0)))
                wins.append((xw["w0"], xw["w1"]))
            if wins:
                _prefetch(rec, wins, out["height"])
        return out

    @r.post("/api/events/{event_id}/clip")
    def set_clip_bounds(event_id: int, body: dict):
        """{clip_in, clip_out}: seconds into the recording; null resets to the pre / post roll."""
        ev = _event(event_id)
        rec = ctx.db.one("SELECT duration_s FROM recordings WHERE id=?", (ev["rec_id"],)) or {}
        dur = float(rec.get("duration_s") or 1e9)
        vals = {}
        for k in ("clip_in", "clip_out"):
            if k in body:
                v = body[k]
                if v is None:
                    vals[k] = None
                else:
                    try:
                        vals[k] = round(max(0.0, min(float(v), dur)), 3)
                    except (TypeError, ValueError):
                        raise app_error(400, "server.player.bad_seconds", "{field} must be a number of seconds or empty.", field=k)
        if not vals:
            raise app_error(400, "server.player.bounds_required", "Give a clip start, a clip end or both.")
        cin = vals.get("clip_in", ev.get("clip_in"))
        cout = vals.get("clip_out", ev.get("clip_out"))
        if cin is not None and cout is not None and cout <= cin:
            raise app_error(400, "server.player.out_before_in", "The clip end must be after the clip start.")
        sets = ", ".join(f"{k}=?" for k in vals)
        ctx.db.execute(f"UPDATE events SET {sets} WHERE id=?", (*vals.values(), event_id))
        ctx.bus.publish("events", action="updated", rec_id=ev["rec_id"], event_id=event_id)
        from server.routes.detect import _ev
        return _ev(ctx.db.one("SELECT * FROM events WHERE id=?", (event_id,)))

    @r.get("/api/clips/{name}")
    def clip_file(name: str):
        if not clips.valid_name(name):
            raise app_error(404, "server.no_clip", "That clip does not exist anymore.")
        p = paths.CLIPS_DIR / name
        if not p.is_file():
            raise app_error(404, "server.player.clip_expired", "This clip was cleaned up. Open the event again.")
        return FileResponse(p, media_type="video/mp4", headers={"Cache-Control": "private, max-age=3600"})

    # ---- whole-recording proxies ----------------------------------------------------------
    @r.post("/api/recordings/{rec_id}/proxy")
    def queue_proxy(rec_id: int):
        rec = _rec(rec_id)
        jid = ctx.jobs.enqueue(rec_id, "proxy", priority=_priority(parse_name(rec["name"])[1], rec["mtime"]) + 20)
        return {"ok": True, "job": jid, "already_queued": jid is None}

    @r.post("/api/proxies/build-all")
    def proxies_all(only_with_events: bool = True):
        """Recordings with no proxy, or one whose audio mix is out of date."""
        where = "status='probed' AND missing=0"
        if only_with_events:
            where += " AND id IN (SELECT DISTINCT rec_id FROM events WHERE status != 'false')"
        rows = [rec for rec in ctx.db.query(f"SELECT * FROM recordings WHERE {where}")
                if not rec.get("proxy_path") or clips.proxy_stale(ctx.db, rec, ctx.cfg)]
        n = sum(1 for rec in rows
                if ctx.jobs.enqueue(rec["id"], "proxy", priority=_priority(parse_name(rec["name"])[1], rec["mtime"])) is not None)
        return {"ok": True, "queued": n, "candidates": len(rows)}

    @r.get("/api/recordings/{rec_id}/proxy")
    def proxy_file(rec_id: int):
        rec = ctx.db.one("SELECT proxy_path FROM recordings WHERE id=?", (rec_id,))
        if rec is None:
            raise app_error(404, "server.no_recording", "That recording does not exist anymore.")
        p = rec.get("proxy_path")
        from pathlib import Path
        if not p or not Path(p).is_file():
            raise app_error(404, "server.player.no_proxy", "No preview copy has been made of this recording yet.")
        return FileResponse(p, media_type="video/mp4", headers={"Cache-Control": "private, max-age=600"})

    # ---- per-track stems for the track panel (media/stems.py) ------------------------------
    def _manifest_out(key: str, man: dict) -> dict:
        out = dict(man)
        out["key"] = key
        out["tracks"] = [dict(t, url=f"/api/stems/{key}/{t['file']}") for t in man.get("tracks", [])]
        return out

    @r.get("/api/recordings/{rec_id}/stems")
    async def rec_stems(rec_id: int, t0: float = 0.0, t1: float = 0.0, full: int = 0):
        """The tracks of a window (the clip's t0 / t1) -- rendered on demand, cached -- or, with
        full=1, of the whole recording (written by the proxy job)."""
        from media import stems
        rec = _rec(rec_id)
        if full:
            info = stems.track_info(ctx.db, rec, ctx.cfg)
            key = stems.full_key(rec)
            man = stems.read_manifest(key) if key else None
            if man is None:
                raise app_error(404, "server.player.no_full_tracks", "The tracks of the whole recording are not ready yet. Make (or remake) the preview copy first.")
            return _manifest_out(key, stems.freshen(man, info))
        if t1 <= t0:
            raise app_error(400, "server.player.bad_window", "The end time must be after the start time.")
        try:
            key, man = await run_in_threadpool(stems.render_window, ctx.db, ctx.cfg, rec, t0, t1)
        except stems.StemError as e:
            raise app_error(503, "server.player.tracks_failed", "The audio tracks could not be prepared: {error}", error=str(e))
        return _manifest_out(key, man)

    @r.get("/api/stems/{key}/{name}")
    def stem_file(key: str, name: str):
        from media import stems
        if not stems.valid_key(key) or not stems.valid_file(name):
            raise app_error(404, "server.player.no_track", "That audio track does not exist.")
        p = stems.stems_root() / key / name
        if not p.is_file():
            raise app_error(404, "server.player.track_expired", "This audio track was cleaned up. Load it again.")
        return FileResponse(p, media_type="audio/mp4", headers={"Cache-Control": "private, max-age=3600"})

    @r.get("/api/clips-status")
    def clips_status():
        cache = clips.cache_usage()               # OSError-safe: a clip may vanish mid-scan (prune / clear)
        return {"n": cache["items"], "bytes": cache["bytes"], "cap_bytes": clips.CLIPS_CAP_BYTES,
                "prefetching": len(prefetching), "utc": time.time(), "cache": cache}

    @r.post("/api/clips-cache/clear")
    def clips_cache_clear():
        # clips + window stems only; proxies and whole-recording stems stay (they are a build, not a cache)
        return {"ok": True, **clips.clear_cache(), "cache": clips.cache_usage()}

    return r
