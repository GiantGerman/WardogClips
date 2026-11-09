"""Stream VOD import (media/vods.py): look at a link, queue its download, list the provider folders.

queue_vod_import() is the import itself, shared by POST /api/vods and the VOD browser's multi-import
(server/routes/vodbrowse.py), so both refuse and dedupe the same way."""

import json
import os
import time

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool

from core.messages import annotate_all, http_error
from media import vods
from utils.i18n import AppError, app_error, with_msg


def already_imported(ctx, meta: dict) -> dict | None:
    """The recording (or file) this VOD already is, if it was imported before and is still there (a
    deleted / moved-away import can be downloaded again)."""
    rows = ctx.db.query("SELECT id, name, path FROM recordings WHERE missing=0 AND (source_url=? OR name LIKE ?)"
                        " ORDER BY id DESC",
                        (meta.get("url"), f"VOD_{meta.get('provider')}_%_{vods._ID_RE.sub('', str(meta.get('id') or ''))[:40]}.mp4"))
    for row in rows:
        if os.path.exists(row["path"]):
            return {"rec_id": row["id"], "name": row["name"], "path": row["path"]}
    p = vods.existing(ctx.cfg, meta.get("provider") or "", meta.get("id") or "")
    return {"rec_id": None, "name": p.name, "path": str(p)} if p else None


def queued_download(ctx, meta: dict) -> int | None:
    """A download of this VOD (same site + id, whatever the pasted link looked like) already waiting / running."""
    row = ctx.db.one("SELECT id FROM jobs WHERE stage='download' AND state IN ('queued','running')"
                     " AND json_extract(arg, '$.provider')=? AND json_extract(arg, '$.id')=?",
                     (meta.get("provider"), str(meta.get("id") or "")))
    return row["id"] if row else None


def queue_vod_import(ctx, url: str, meta: dict | None = None, again: bool = False) -> dict:
    """Queue one VOD's download. Blocking (inspect asks the site, ~1-3 s): call it from a worker thread.
    Returns {ok: True, job, already_queued, already, meta, dest}, or when refused {ok: False, error,
    code (400 = bad link / the site refused it, 409 = imported before), already}. A meta from
    /api/vods/inspect saves the second look when it belongs to the link's site."""
    try:
        url, provider = vods.check_url(str(url or (meta or {}).get("url") or ""))
        if not isinstance(meta, dict) or meta.get("provider") != provider or not meta.get("id"):
            meta = vods.inspect(ctx.cfg, url)
    except vods.VodError as e:
        return {"ok": False, "error": str(e), "code": 400, "already": None}
    meta = dict(meta)
    meta.pop("already", None)
    meta.pop("dest", None)
    dest = str(vods.provider_dir(ctx.cfg, meta["provider"], create=False))
    have = already_imported(ctx, meta)
    if have and not again:
        return with_msg({"ok": False, "code": 409, "already": have, "meta": meta, "dest": dest}, "error",
                        "server.vods.already_imported", "This VOD was already imported as {name}.", name=have["name"])
    q = queued_download(ctx, meta)
    if q is not None:
        return {"ok": True, "job": q, "already_queued": True, "already": have, "meta": meta, "dest": dest}
    if not meta.get("start_utc"):
        meta["start_utc"] = time.time()          # unknown start: fixed now, so a retry reuses the same file name
    vods.provider_dir(ctx.cfg, meta["provider"])        # the provider folder exists from the moment of the import
    arg = json.dumps({k: meta.get(k) for k in ("url", "provider", "id", "title", "channel", "channel_key", "channel_url",
                                                "start_utc", "duration_s", "height", "fps", "vcodec", "size_bytes",
                                                "parts", "was_live")},
                     sort_keys=True)
    jid = ctx.jobs.enqueue(None, "download", priority=int(time.time() // 60) + 50, arg=arg)
    return {"ok": True, "job": jid, "already_queued": jid is None, "already": have, "meta": meta, "dest": dest}


def build(ctx) -> APIRouter:
    r = APIRouter()

    def _enabled():
        if not ctx.cfg.vods.enabled:
            raise app_error(409, "server.vods_off", "VOD import is off (Settings > VODs).")

    @r.get("/api/vods")
    def vods_status():
        root = vods.vod_root(ctx.cfg)
        provs = []
        for name in vods.PROVIDER_NAMES:
            d = root / name
            files = sorted(d.glob("VOD_*.mp4")) if d.is_dir() else []
            provs.append({"provider": name, "dir": str(d), "exists": d.is_dir(), "files": len(files),
                          "bytes": sum(f.stat().st_size for f in files if f.is_file())})
        jobs = ctx.db.query("SELECT id, state, progress, message, error, arg, created_utc, finished_utc FROM jobs"
                            " WHERE stage='download' ORDER BY id DESC LIMIT 20")
        for j in jobs:
            try:
                a = json.loads(j.pop("arg") or "{}")
            except ValueError:
                a = {}
            j.update({k: a.get(k) for k in ("url", "provider", "title", "channel")})
        annotate_all(jobs, "message", "error")      # translatable job texts: <field>_i18n
        return {"enabled": ctx.cfg.vods.enabled, "available": vods.available(), "root": str(root),
                "in_library": any(str(root).lower() == str(x).lower() for x in ctx.cfg.library.roots),
                "providers": provs, "jobs": jobs, "max_height": ctx.cfg.vods.max_height}

    @r.post("/api/vods/inspect")
    async def vods_inspect(body: dict):
        """What a link is (title, channel, date, length, quality, size) -- nothing downloaded."""
        _enabled()
        try:
            meta = await run_in_threadpool(vods.inspect, ctx.cfg, str((body or {}).get("url") or ""))
        except vods.VodError as e:
            raise http_error(400, e)
        meta["already"] = already_imported(ctx, meta)
        meta["dest"] = str(vods.provider_dir(ctx.cfg, meta["provider"], create=False))
        return meta

    @r.post("/api/vods")
    async def vods_import(body: dict):
        """Queue the download: {url} (looked at first) or {url, meta from /inspect}. A VOD imported before
        is not downloaded again (409 with where it is)."""
        _enabled()
        b = body or {}
        res = await run_in_threadpool(queue_vod_import, ctx, str(b.get("url") or ""),
                                      b.get("meta") if isinstance(b.get("meta"), dict) else None, bool(b.get("again")))
        if not res["ok"]:
            meta = res.get("error_i18n")
            if meta:
                err = AppError(res["code"], meta["code"], res["error"])
                err.params = dict(meta.get("params") or {})
                raise err
            raise http_error(res["code"], vods.VodError(res["error"]))
        res.pop("already", None)                 # only refusals carry 'already'
        return res

    return r
