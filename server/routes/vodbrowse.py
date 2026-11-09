"""VOD browser (media/vodbrowse.py): resolve a pasted link / name, search the sites for a streamer, list their
recent VODs marked in-library / queued, import a selection, and keep favorites.

Every site lookup blocks for 1-25 s (yt-dlp / a page fetch), so those routes are plain `def` (FastAPI runs
them on its thread pool) and never hold the event loop."""

from fastapi import APIRouter

from core.messages import annotate, annotate_all, http_error
from utils.i18n import app_error

from media import vodbrowse, vods
from server.routes.vods import already_imported, queue_vod_import


def build(ctx) -> APIRouter:
    r = APIRouter()

    def _with_fav(listing: dict) -> dict:
        ch = listing["channel"]
        fav = vodbrowse.fav_find(ctx.db, ch["provider"], ch["key"])
        listing["favorite"] = fav is not None
        listing["favorite_id"] = fav["id"] if fav else None
        return listing

    @r.post("/api/vodbrowse/resolve")
    def vb_resolve(body: dict):
        """{text, inspect?} -> parse_link's answer; a VOD link is also inspected for the streamer behind it unless
        inspect is false (then no network: the Import VOD dialog, which has just run inspect itself)."""
        b = body if isinstance(body, dict) else {}
        flag = b.get("inspect")
        inspect = flag is None or not (flag is False or flag == 0
                       or (isinstance(flag, str) and flag.strip().lower() in ("false", "0", "no", "off", "")))
        res = vodbrowse.resolve(ctx.cfg, str(b.get("text") or ""), inspect=inspect)
        if res.get("kind") == "vod" and res.get("meta"):
            res["already"] = already_imported(ctx, res["meta"])
        return annotate(res, "error")

    @r.get("/api/vodbrowse/search")
    def vb_search(q: str = "", providers: str = ""):
        provs = [p.strip() for p in providers.split(",") if p.strip()] or None
        if provs:
            bad = [p for p in provs if p not in vods.PROVIDER_NAMES]
            if bad:
                raise app_error(400, "server.vods.unknown_site", "Unknown site: {sites}", sites=", ".join(bad))
        return {"q": q, "results": annotate_all(vodbrowse.search(ctx.cfg, q, provs), "error")}

    @r.get("/api/vodbrowse/channel")
    def vb_channel(provider: str, key: str, limit: int = 30, refresh: int = 0):
        try:
            listing = vodbrowse.list_vods(ctx.cfg, provider, key, limit=limit, refresh=bool(refresh), db=ctx.db)
        except vodbrowse.BrowseError as e:
            raise http_error(400, e)
        return _with_fav(annotate(listing, "error"))

    @r.post("/api/vodbrowse/import")
    def vb_import(body: dict):
        """{items: [{url, meta?}]} -> one result per item; one bad link never stops the rest."""
        if not ctx.cfg.vods.enabled:
            raise app_error(409, "server.vods_off", "VOD import is off (Settings > VODs).")
        items = (body or {}).get("items") or []
        if not isinstance(items, list):
            raise app_error(400, "server.bad_request", "The request was not understood.")
        results = []
        for it in items[:200]:
            url = str((it or {}).get("url") or "") if isinstance(it, dict) else str(it or "")
            try:
                res = queue_vod_import(ctx, url, (it.get("meta") if isinstance(it, dict) else None))
            except Exception as e:                       # noqa: BLE001 -- reported on that row only
                res = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
            row = {"url": url, "ok": res["ok"]}
            if res.get("job"):
                row["job"] = res["job"]
            if res.get("already_queued"):
                row["already_queued"] = True
            if res.get("already"):
                row["already"] = res["already"]
            if res.get("error"):
                row["error"] = res["error"]
                if res.get("error_i18n"):
                    row["error_i18n"] = res["error_i18n"]
                else:
                    annotate(row, "error")
            if res.get("meta"):
                row["id"] = res["meta"].get("id")
                row["title"] = res["meta"].get("title")
            results.append(row)
        return {"results": results, "queued": sum(1 for x in results if x["ok"])}

    @r.get("/api/vodbrowse/favorites")
    def vb_favs():
        return {"favorites": vodbrowse.fav_list(ctx.db)}

    @r.post("/api/vodbrowse/favorites")
    def vb_fav_add(body: dict):
        b = body or {}
        try:
            fav, created = vodbrowse.fav_add(ctx.db, str(b.get("provider") or ""), str(b.get("key") or ""),
                                             str(b.get("name") or ""), str(b.get("url") or ""), str(b.get("note") or ""))
        except ValueError as e:
            raise http_error(400, e)
        return {**fav, "created": created}

    @r.put("/api/vodbrowse/favorites/{fav_id}")
    def vb_fav_put(fav_id: int, body: dict):
        b = body or {}
        fav = vodbrowse.fav_update(ctx.db, fav_id, note=b.get("note"), name=b.get("name"))
        if fav is None:
            raise app_error(404, "server.vods.no_favorite", "That favorite does not exist anymore.")
        return fav

    @r.delete("/api/vodbrowse/favorites/{fav_id}")
    def vb_fav_del(fav_id: int):
        if not vodbrowse.fav_delete(ctx.db, fav_id):
            raise app_error(404, "server.vods.no_favorite", "That favorite does not exist anymore.")
        return {"ok": True}

    @r.post("/api/vodbrowse/favorites/refresh")
    def vb_fav_refresh(body: dict | None = None):
        ids = (body or {}).get("ids") if isinstance(body, dict) else None
        return {"favorites": annotate_all(vodbrowse.refresh_favorites(ctx.cfg, ctx.db, ids=ids), "error")}

    return r
