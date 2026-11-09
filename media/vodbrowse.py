"""VOD browser: find a streamer on YouTube / Twitch / Kick / TikTok / Rumble, list their recent VODs, keep
favorites, and tell which VODs are already in the library or queued -- so importing is "tick and go"
instead of pasting links one by one. The download itself stays media/vods.py's (inspect -> job 'download').

Listing = yt-dlp ``--flat-playlist -J --playlist-end N`` on the streamer's videos tab (one request, no
per-video lookups), with vods._base_cmd() so impersonation and the Node JS runtime apply. What each site
returns (metadata only, about 1 s per listing):

- YouTube ``/<key>/streams`` (past live streams): entries {id, url watch?v=, title, duration, live_status
  was_live | is_live}. A channel that never streamed has no such tab ("This channel does not have a streams
  tab") -> ``/<key>/videos``. Flat entries carry NO date unless ``--extractor-args youtubetab:approximate_date``
  (then ``timestamp`` = the day at 00:00 UTC from "3 days ago" -> ``date_approx``). Unknown handle -> HTTP 404.
  ``channel/<id>`` is CASE-SENSITIVE: a lower-cased id answers HTTP 400 -- so norm_key() keeps the id's case
  (as vods.canon_channel_key does for every other key it lower-cases). Top level: uploader_id '@handle',
  channel, channel_url, channel_follower_count.
- YouTube channel search ``/results?search_query=<q>&sp=EgIQAg%3D%3D`` (filter: channels): entries {channel,
  channel_id, uploader_id '@handle' (null for '- Topic' channels), channel_follower_count, description} -> up
  to 5 extra search hits.
- Twitch ``/<login>/videos?filter=archives``: entries {id 'v<n>' (= inspect's id), url /videos/<n>, title,
  duration, thumbnail}, no date field -- but the thumbnail path ends in ``<login>_<stream id>_<epoch>/thumb/``,
  within seconds of inspect's timestamp -> used as start_utc. An unknown login answers rc 0 with 0 entries
  (same as a channel with no archives), so an empty list is followed by ``twitch.tv/<login>``: "does not
  exist" vs "The channel is not currently live".
- Kick ``/<slug>/videos``: entries {id uuid, url (with a ``#__youtubedl_smuggle=`` fragment -- stripped),
  title, duration, timestamp, is_live, channel, uploader_id}. Unknown slug -> HTTP 404.
- Rumble: yt-dlp's RumbleChannel ignores the tab (its URL regex keeps only ``/c/<name>``) and pages through the
  whole channel, taking tens of seconds for a handful of entries without titles or ids. Instead the page
  itself is fetched (curl_cffi with chrome impersonation -- the transport yt-dlp uses for Rumble; 403 without
  it): ``/c/<name>/livestreams`` has one ``<script type="application/json">`` whose ``items`` hold objects
  {object_type 'video', url, permalink_id, title, duration, upload_date ISO, live_streamed_on ISO (the stream
  start), live, live_placeholder, livestream_status, thumb, by {name, relative_url '/c/<name>'}}. NOTE
  inspect's id for a Rumble VOD is the EMBED id, which differs from the page's permalink id, so Rumble VODs
  are matched to the library by URL. 429 = "Rumble is rate-limiting, try again in a minute".
- TikTok profiles: yt-dlp fails ("tiktok: Failed to parse JSON") -> a clear message; single video links
  still import.

Nothing here downloads media or touches the footage; listings are cached in memory for 5 minutes.
"""

import html as html_mod
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import parse_qs, quote_plus, urlparse

from media import vods
from utils.i18n import msg as _msg

logger = logging.getLogger("wclips.vodbrowse")

CACHE_S = 300.0
LIST_TIMEOUT = 60.0
SEARCH_TIMEOUT = 25.0
TIKTOK_BROKEN = _msg("server.vods.tiktok_broken", "TikTok profile listing is not available right now -- paste single video links")["message"]
RUMBLE_429 = _msg("server.vods.rumble_429", "Rumble is rate-limiting, try again in a minute")["message"]

LISTING_URLS = {           # tried in order; the next one only when the first is missing / empty
    "YouTube": ("https://www.youtube.com/{key}/streams", "https://www.youtube.com/{key}/videos"),
    "Twitch": ("https://www.twitch.tv/{key}/videos?filter=archives",),
    "Kick": ("https://kick.com/{key}/videos",),
    "Rumble": ("https://rumble.com/{key}/livestreams", "https://rumble.com/{key}"),
    "TikTok": ("https://www.tiktok.com/{key}",),
}
YT_SEARCH = "https://www.youtube.com/results?search_query={q}&sp=EgIQAg%3D%3D"
_NOT_LIVE = {"is_live", "is_upcoming", "post_live"}

_cache: dict = {}
_cache_lock = threading.Lock()


class BrowseError(RuntimeError):
    """A listing that failed; the message is for the page."""


# ---- links ------------------------------------------------------------------------------------------
_TWITCH_RESERVED = {"videos", "directory", "downloads", "jobs", "p", "settings", "search", "subscriptions", "inventory",
                    "wallet", "friends", "turbo", "prime", "store", "bits", "login", "signup", "messages", "drops",
                    "following", "moderator", "popout", "embed", "team", "collections", "u", "event", "clips",
                    "broadcast", "dashboard", "privacy", "legal", "terms", "user", "help"}
_KICK_RESERVED = {"categories", "category", "browse", "following", "search", "video", "dashboard", "terms-of-service",
                  "privacy-policy", "community-guidelines", "login", "signup", "auth", "settings", "subscriptions",
                  "transactions", "notifications", "messages", "popout", "embed", "dmca-policy", "clips"}
_TWITCH_LOGIN = re.compile(r"[a-z0-9_]{2,25}")
_KICK_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{1,39}")


def norm_key(provider: str, key: str) -> str:
    """The streamer key as vods.channel_identity writes it (lower case), except a YouTube 'channel/<id>':
    channel ids are case-sensitive (lower-cased -> HTTP 400)."""
    k = (key or "").strip().strip("/")
    if provider == "YouTube":
        if k.lower().startswith("channel/"):
            return "channel/" + k.split("/", 1)[1]
        if k.lower().startswith(("c/", "user/")) or k.startswith("@"):
            return k.lower()
        return "@" + k.lower() if k else ""
    if provider == "TikTok":
        return "@" + k.lstrip("@").lower() if k else ""
    if provider == "Rumble":
        k = k.lower()
        return k if (not k or k.startswith(("c/", "user/"))) else "c/" + k
    return k.lstrip("@").lower()


def channel_url(provider: str, key: str) -> str:
    return vods.CHANNEL_URLS[provider].format(key=key) if key else ""


def _looks_like_url(text: str) -> bool:
    if re.match(r"^https?://", text, re.I):
        return True
    return vods.provider_of("https://" + text) is not None


def parse_link(text: str) -> dict:
    """What a pasted text is: {kind 'vod' | 'channel' | 'name' | 'unknown', provider, key, url, id?}.
    No network. Keys as norm_key(); fragments (yt-dlp's '#__youtubedl_smuggle=') and tracking queries dropped."""
    t = (text or "").strip()
    if not t:
        return {"kind": "unknown", "provider": None, "key": None, "url": "",
                "error": _msg("server.vods.type_name", "type a streamer name or paste a link")["message"]}
    if not _looks_like_url(t):
        return {"kind": "name", "provider": None, "key": None, "url": "", "q": t.lstrip("@").strip()}
    try:
        u, prov = vods.check_url(t)
    except vods.VodError as e:
        return {"kind": "unknown", "provider": None, "key": None, "url": t, "error": str(e)}
    p = urlparse(u.split("#", 1)[0])
    host = (p.hostname or "").lower()
    parts = [x for x in p.path.split("/") if x]
    low = [x.lower() for x in parts]
    qs = parse_qs(p.query)
    out = {"kind": "unknown", "provider": prov, "key": None, "url": u.split("#", 1)[0]}

    def vod(vid, key=None, url=None):
        out.update(kind="vod", id=vid, key=norm_key(prov, key) if key else None, url=url or out["url"])
        return out

    def channel(key):
        k = norm_key(prov, key)
        out.update(kind="channel", key=k, url=channel_url(prov, k))
        return out

    if prov == "YouTube":
        if host.endswith("youtu.be") and parts:
            return vod(parts[0], url=f"https://www.youtube.com/watch?v={parts[0]}")
        if low[:1] == ["watch"] and qs.get("v"):
            v = qs["v"][0]
            return vod(v, url=f"https://www.youtube.com/watch?v={v}")
        if len(parts) >= 2 and low[0] in ("live", "shorts", "embed", "v"):
            return vod(parts[1], url=f"https://www.youtube.com/watch?v={parts[1]}")
        if parts and parts[0].startswith("@"):
            return channel(parts[0])
        if len(parts) >= 2 and low[0] == "channel":
            return channel("channel/" + parts[1])
        if len(parts) >= 2 and low[0] in ("c", "user"):
            return channel(f"{low[0]}/{parts[1]}")
    elif prov == "Twitch":
        login_ok = bool(parts) and low[0] not in _TWITCH_RESERVED and bool(_TWITCH_LOGIN.fullmatch(low[0]))
        if host.startswith("clips.") and parts:
            return vod(parts[0])
        if len(parts) >= 2 and low[0] == "videos" and parts[1].isdigit():
            return vod("v" + parts[1], url=f"https://www.twitch.tv/videos/{parts[1]}")
        if len(parts) >= 3 and low[1] in ("v", "video") and parts[2].isdigit():     # old /<login>/v/<n> links
            return vod("v" + parts[2], key=parts[0] if login_ok else None,
                       url=f"https://www.twitch.tv/videos/{parts[2]}")
        if len(parts) >= 3 and low[1] == "clip" and login_ok:
            return vod(parts[2], key=parts[0], url=f"https://www.twitch.tv/{low[0]}/clip/{parts[2]}")
        if login_ok and (len(parts) == 1 or low[1] not in ("v", "video", "clip")):
            return channel(parts[0])
    elif prov == "Kick":
        slug_ok = bool(parts) and low[0] not in _KICK_RESERVED and bool(_KICK_SLUG.fullmatch(low[0]))
        if len(parts) >= 2 and low[0] == "video":
            return vod(parts[1], url=f"https://kick.com/video/{parts[1]}")
        if slug_ok and len(parts) >= 3 and low[1] == "videos":
            return vod(parts[2], key=parts[0], url=f"https://kick.com/{low[0]}/videos/{parts[2]}")
        clip = parts[2] if (slug_ok and len(parts) >= 3 and low[1] == "clips") else \
            ((qs.get("clip") or [""])[0].strip() if slug_ok else "")
        if clip:                                         # /<slug>/clips/<id> or /<slug>?clip=<id>
            return vod(clip, key=parts[0], url=f"https://kick.com/{low[0]}/clips/{clip}")
        if slug_ok and (len(parts) == 1 or low[1] in ("videos", "clips", "about", "chatroom")):
            return channel(parts[0])
    elif prov == "TikTok":
        if len(parts) >= 3 and parts[0].startswith("@") and low[1] in ("video", "photo"):
            return vod(parts[2], key=parts[0], url=f"https://www.tiktok.com/{low[0]}/{low[1]}/{parts[2]}")
        if host.startswith(("vm.", "vt.")) or (low[:1] == ["t"] and len(parts) >= 2):
            return vod(parts[-1])                        # a share short link: the channel comes from inspect
        if parts and parts[0].startswith("@"):
            return channel(parts[0])
    elif prov == "Rumble":
        if parts and re.match(r"^v[a-z0-9]+-.*\.html$", low[0]):
            return vod(low[0].split("-", 1)[0], url=f"https://rumble.com/{parts[0]}")
        if len(parts) >= 2 and low[0] == "embed":
            return vod(parts[1])
        if len(parts) >= 2 and low[0] in ("c", "user"):
            return channel(f"{low[0]}/{parts[1]}")
    out["error"] = f"that {prov} link is neither a VOD nor a streamer's page"
    return out


def url_key(url: str) -> str:
    """A comparable form of a VOD link: host without www./m., path, and only YouTube's v= kept."""
    try:
        p = urlparse((url or "").strip().split("#", 1)[0])
    except ValueError:
        return ""
    host = (p.hostname or "").lower()
    for pre in ("www.", "m."):
        if host.startswith(pre):
            host = host[len(pre):]
    path = p.path.rstrip("/")
    v = parse_qs(p.query).get("v") if "youtube" in host else None
    return f"{host}{path.lower() if 'youtube' not in host else path}" + (f"?v={v[0]}" if v else "")


def _clean_url(url: str, provider: str) -> str:
    u = (url or "").split("#", 1)[0]
    if provider in ("Rumble", "Kick", "Twitch", "TikTok"):
        u = u.split("?", 1)[0]
    return u


# ---- runners (the tests replace these two) ----------------------------------------------------------
def _run(provider: str, url: str, limit: int, timeout: float, extra: tuple = ()) -> dict:
    """yt-dlp's flat listing of one URL (no download)."""
    cmd = vods._base_cmd(provider) + list(extra) + ["--flat-playlist", "-J", "--playlist-end", str(int(limit)), "--", url]
    return vods._run_json(cmd, timeout, provider)


def _http_get(provider: str, url: str, timeout: float) -> tuple[int, str]:
    """One page, fetched like yt-dlp would (browser impersonation where the site insists)."""
    try:
        from curl_cffi import requests as creq
    except ImportError:
        raise BrowseError(f"{provider} needs browser impersonation: install it with "
                          "venv\\Scripts\\python.exe -m pip install \"yt-dlp[default,curl-cffi]\"")
    try:
        r = creq.get(url, impersonate=vods.IMPERSONATE.get(provider, "chrome"), timeout=timeout)
    except Exception as e:                                       # noqa: BLE001 -- network errors of any kind
        raise BrowseError(f"{provider} did not answer: {str(e)[:160]}")
    return r.status_code, r.text


# ---- listing ----------------------------------------------------------------------------------------
def _friendly(provider: str, key: str, err: str) -> str:
    low = err.lower()
    if provider == "TikTok":
        return TIKTOK_BROKEN
    if "429" in low or "too many requests" in low:
        return RUMBLE_429 if provider == "Rumble" else f"{provider} is rate-limiting, try again in a minute"
    if "404" in low or "does not exist" in low or "not found" in low:
        return f"no {provider} channel '{key}'"
    return err


def _start_of(provider: str, e: dict) -> tuple[float | None, bool]:
    """(start epoch s or None, approximate?) from a flat entry."""
    ts = vods.start_utc(e)
    if ts:
        return ts, provider == "YouTube"                # YouTube's flat dates are 'N days ago' -> 00:00 UTC
    if provider == "Twitch":
        m = re.search(r"_(\d{10})/+thumb/", str(e.get("thumbnail") or ""))
        if m and 1.2e9 < int(m.group(1)) < time.time() + 86400:
            return float(m.group(1)), False
    return None, False


def _entry(provider: str, e: dict) -> dict | None:
    url = _clean_url(e.get("url") or e.get("webpage_url") or "", provider)
    vid = str(e.get("id") or "")
    if not url and not vid:
        return None
    if provider == "Twitch" and vid.isdigit():
        vid = "v" + vid
    status = e.get("live_status") or ("is_live" if e.get("is_live") else None)
    dur = e.get("duration")
    live = status == "is_live" or (provider == "Kick" and dur is not None and float(dur) <= 0)
    start, approx = _start_of(provider, e)
    thumb = e.get("thumbnail")
    if not thumb and isinstance(e.get("thumbnails"), list) and e["thumbnails"]:
        thumb = (e["thumbnails"][-1] or {}).get("url")
    return {"id": vid, "url": url, "title": e.get("title") or "", "duration_s": float(dur) if dur else None,
            "start_utc": start, "date_approx": approx, "live_status": status, "live": bool(live),
            "importable": not live and status not in _NOT_LIVE, "thumbnail": thumb}


def _iso(s) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() if s else None
    except ValueError:
        return None


def _rumble_items(html: str) -> list[dict]:
    for m in re.finditer(r'<script type="application/json">(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return [x for x in data["items"] if isinstance(x, dict) and x.get("object_type") == "video"]
    return []


def _list_rumble(key: str, limit: int, timeout: float) -> dict:
    items, chan, last_err, title = [], {}, None, ""
    for tpl in LISTING_URLS["Rumble"]:
        code, html = _http_get("Rumble", tpl.format(key=key), timeout)
        if code == 429:
            raise BrowseError(RUMBLE_429)
        if code == 404:
            last_err = f"no Rumble channel '{key}'"
            break
        if code != 200:
            last_err = f"Rumble answered HTTP {code}"
            continue
        items = _rumble_items(html)
        m = re.search(r"<title>(.*?)</title>", html, re.S)
        title = title or (html_mod.unescape(m.group(1)).strip() if m else "")
        if items:
            break
    if not items and last_err:
        raise BrowseError(last_err)
    vods_ = []
    for x in items[:limit]:
        by = x.get("by") or {}
        if not chan and by.get("name"):
            chan = {"name": by["name"], "key": (by.get("relative_url") or "").strip("/").lower()}
        live = bool(x.get("live"))
        upcoming = bool(x.get("live_placeholder")) and not live
        start = _iso(x.get("live_streamed_on")) or _iso(x.get("upload_date"))
        vods_.append({"id": str(x.get("permalink_id") or ""), "url": _clean_url(x.get("url") or "", "Rumble"),
                      "title": x.get("title") or "", "duration_s": float(x["duration"]) if x.get("duration") else None,
                      "start_utc": start, "date_approx": False,
                      "live_status": "is_live" if live else "is_upcoming" if upcoming else
                      ("was_live" if x.get("live_streamed_on") else "not_live"),
                      "live": live, "importable": not live and not upcoming and not x.get("is_processing"),
                      "thumbnail": x.get("thumb")})
    return {"name": chan.get("name") or title or key, "key": chan.get("key") or key, "vods": vods_}


def _list_ytdlp(provider: str, key: str, limit: int, timeout: float) -> dict:
    extra = ("--extractor-args", "youtubetab:approximate_date") if provider == "YouTube" else ()
    info, last_err = None, None
    for tpl in LISTING_URLS[provider]:
        try:
            info = _run(provider, tpl.format(key=key), limit, timeout, extra)
        except vods.VodError as e:
            last_err = str(e)
            if provider == "YouTube" and "streams tab" in last_err.lower():
                continue                                # never streamed: its uploads then
            raise BrowseError(_friendly(provider, key, last_err))
        if info.get("entries"):
            break
    if info is None:
        raise BrowseError(_friendly(provider, key, last_err or "no listing"))
    vods_ = [v for v in (_entry(provider, e) for e in (info.get("entries") or [])[:limit] if isinstance(e, dict)) if v]
    name, ckey = "", key
    if provider == "YouTube":
        name = info.get("channel") or info.get("uploader") or ""
        up = str(info.get("uploader_id") or "")
        if up.startswith("@"):
            ckey = up.lower()
    elif provider == "Kick":
        first = next((e for e in info.get("entries") or [] if isinstance(e, dict)), {})
        name = first.get("channel") or first.get("uploader") or ""
    if not name:
        name = re.sub(r"\s+-\s+(Past Broadcasts|Videos|All Videos).*$", "", str(info.get("title") or "")) or key
    if provider == "Twitch" and not vods_:
        try:                                            # no archives, or no such login? (rc 0 either way)
            live = _run(provider, f"https://www.twitch.tv/{key}", 1, timeout)
            if live.get("is_live"):
                vods_.append({"id": str(live.get("id") or ""), "url": f"https://www.twitch.tv/{key}",
                              "title": live.get("description") or live.get("title") or "live now", "duration_s": None,
                              "start_utc": vods.start_utc(live), "date_approx": False, "live_status": "is_live",
                              "live": True, "importable": False, "thumbnail": live.get("thumbnail")})
        except vods.VodError as e:
            if "does not exist" in str(e).lower():
                raise BrowseError(_msg("server.vods.no_twitch_channel", "no Twitch channel '{name}'", name=key)["message"])
    return {"name": name, "key": ckey, "vods": vods_}


def _fetch(cfg, provider: str, key: str, limit: int, timeout: float) -> dict:
    if not vods.available():
        raise BrowseError(vods.NO_YTDLP)
    if provider == "Rumble":
        return _list_rumble(key, limit, timeout)
    if provider in vods.IMPERSONATE and not vods.impersonation_available():
        raise BrowseError(f"{provider} needs browser impersonation: install it with "
                          "venv\\Scripts\\python.exe -m pip install \"yt-dlp[default,curl-cffi]\"")
    try:
        return _list_ytdlp(provider, key, limit, timeout)
    except BrowseError as e:
        if provider == "TikTok":
            raise BrowseError(TIKTOK_BROKEN) from e
        raise


def list_vods(cfg, provider: str, key: str, limit: int = 30, refresh: bool = False, db=None,
              timeout: float = LIST_TIMEOUT) -> dict:
    """{channel: {provider, key, name, url}, vods: [...], error?, cached}. With db, every VOD is marked
    imported / queued (always fresh: only the site's answer is cached, 5 min per (provider, key, limit))."""
    if provider not in vods.PROVIDER_NAMES:
        raise BrowseError(f"unknown site '{provider}'")
    key = norm_key(provider, key)
    if not key:
        raise BrowseError(_msg("server.vods.no_streamer", "no streamer given")["message"])
    limit = max(1, min(int(limit or 30), 100))
    ck = (provider, key, limit)
    with _cache_lock:
        hit = _cache.get(ck)
    cached = bool(hit and not refresh and time.monotonic() - hit[0] < CACHE_S)
    if cached:
        raw = hit[1]
    else:
        t0 = time.monotonic()
        try:
            raw = _fetch(cfg, provider, key, limit, timeout)
        except BrowseError as e:
            logger.info("[VODBrowse] list failed provider=%s key=%s err=%s", provider, key, str(e)[:160])
            return {"channel": {"provider": provider, "key": key, "name": key, "url": channel_url(provider, key)},
                    "vods": [], "error": str(e), "cached": False}
        raw["fetched_utc"] = time.time()
        with _cache_lock:
            _cache[ck] = (time.monotonic(), raw)
        logger.info("[VODBrowse] listed provider=%s key=%s n=%d secs=%.1f", provider, key, len(raw["vods"]),
                    time.monotonic() - t0)
    ckey = norm_key(provider, raw.get("key") or key)
    out = {"channel": {"provider": provider, "key": ckey, "name": raw.get("name") or ckey, "url": channel_url(provider, ckey)},
           "vods": [dict(v) for v in raw["vods"]], "cached": cached, "fetched_utc": raw.get("fetched_utc")}
    if db is not None:
        mark(db, provider, out["vods"])
    return out


def _seen_ids(vl: list[dict]) -> list[str]:
    """The ids a favorite remembers as seen: importable (finished) VODs only. A live stream or an upcoming
    placeholder must count as new once it is a finished VOD (YouTube keeps the id), and a placeholder at the
    top of a listing must not hide everything below it."""
    return [v["id"] for v in vl if v.get("id") and v.get("importable")]


def cached_ids(provider: str, key: str, importable_only: bool = False) -> list[str] | None:
    """VOD ids of the freshest cached listing of this streamer (any limit), or None -- no network.
    importable_only: only the finished VODs (what a favorite stores as seen)."""
    key = norm_key(provider, key)
    best = None
    with _cache_lock:
        for (p, k, _), (t, raw) in _cache.items():
            if p == provider and (k == key or norm_key(p, raw.get("key") or "") == key) and (best is None or t > best[0]):
                best = (t, raw)
    if best is None:
        return None
    vl = best[1]["vods"]
    return _seen_ids(vl) if importable_only else [v["id"] for v in vl if v.get("id")]


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _file_id(name: str) -> str:
    """'VOD_Twitch_2026-11-05_20-14-03_v2612345678.mp4' -> 'v2612345678'."""
    stem = name[:-4] if name.lower().endswith(".mp4") else name
    bits = stem.split("_", 4)
    return bits[4] if len(bits) == 5 else ""


def mark(db, provider: str, items: list[dict]) -> None:
    """imported / rec_id: a library recording with this VOD's link or id (a file deleted from disk does not
    count -- it can be imported again); queued / job_id: a waiting or running download of it."""
    by_id, by_url = {}, {}
    for r in db.query("SELECT id, name, source_url FROM recordings WHERE missing=0 AND kind='vod' AND name LIKE ?",
                      (f"VOD_{provider}_%",)):
        fid = _file_id(r["name"])
        if fid:
            by_id.setdefault(fid, r["id"])
        if r.get("source_url"):
            by_url.setdefault(url_key(r["source_url"]), r["id"])
    jobs_id, jobs_url = {}, {}
    for j in db.query("SELECT id, json_extract(arg, '$.id') vid, json_extract(arg, '$.url') url FROM jobs"
                      " WHERE stage='download' AND state IN ('queued','running') AND json_extract(arg, '$.provider')=?",
                      (provider,)):
        if j.get("vid"):
            jobs_id.setdefault(str(j["vid"]), j["id"])
        if j.get("url"):
            jobs_url.setdefault(url_key(j["url"]), j["id"])
    for v in items:
        fid = vods._ID_RE.sub("", str(v.get("id") or ""))[:40]
        uk = url_key(v.get("url") or "")
        rec = by_id.get(fid) if fid else None
        rec = rec or (by_url.get(uk) if uk else None)
        job = (jobs_id.get(str(v.get("id"))) if v.get("id") else None) or (jobs_url.get(uk) if uk else None)
        v.update(imported=rec is not None, rec_id=rec, queued=job is not None, job_id=job)


# ---- search -----------------------------------------------------------------------------------------
def name_key(provider: str, q: str) -> str:
    """A plain name -> the key it would have on that site (YouTube '@q', Twitch q, Kick q, TikTok '@q',
    Rumble 'c/q'); spaces dropped."""
    compact = re.sub(r"\s+", "", (q or "").strip().lstrip("@"))
    if not compact:
        return ""
    return {"YouTube": "@" + compact, "TikTok": "@" + compact, "Rumble": "c/" + compact}.get(provider, compact).lower()


def _hit(listing: dict) -> dict:
    ch = listing["channel"]
    vl = listing.get("vods") or []
    row = {"provider": ch["provider"], "key": ch["key"], "name": ch["name"], "url": ch["url"],
           "ok": not listing.get("error"), "sample": [v["title"] for v in vl[:3]], "n": len(vl)}
    if listing.get("error"):
        row["error"] = listing["error"]
    return row


def youtube_channels(q: str, limit: int = 5, timeout: float = SEARCH_TIMEOUT) -> list[dict]:
    """Channel matches from YouTube's search page (channels filter)."""
    try:
        info = _run("YouTube", YT_SEARCH.format(q=quote_plus(q)), limit, timeout)
    except vods.VodError as e:
        logger.info("[VODBrowse] youtube channel search failed q=%s err=%s", q, str(e)[:160])
        return []
    out = []
    for e in (info.get("entries") or [])[:limit]:
        if not isinstance(e, dict) or e.get("ie_key") not in (None, "YoutubeTab"):
            continue
        up = str(e.get("uploader_id") or "")
        key = up.lower() if up.startswith("@") else ("channel/" + e["channel_id"] if e.get("channel_id") else "")
        if not key:
            continue
        out.append({"provider": "YouTube", "key": key, "name": e.get("channel") or e.get("title") or key,
                    "url": channel_url("YouTube", key), "ok": True, "sample": [], "n": None, "match": "search",
                    "followers": e.get("channel_follower_count"), "description": (e.get("description") or "")[:160]})
    return out


def search(cfg, q: str, providers=None, timeout: float = SEARCH_TIMEOUT) -> list[dict]:
    """A streamer name on every selected site at once (each a 3-VOD listing), plus YouTube's channel search.
    A link goes through parse_link / resolve instead."""
    q = (q or "").strip()
    if not q:
        return []
    provs = [p for p in (providers or vods.PROVIDER_NAMES) if p in vods.PROVIDER_NAMES]
    link = parse_link(q)
    if link["kind"] in ("vod", "channel"):
        r = resolve(cfg, q)
        ch = r.get("channel")
        if not ch or not ch.get("key"):
            return [{"provider": link["provider"], "key": None, "name": q, "url": link.get("url") or "", "ok": False,
                     "error": r.get("error") or _msg("server.vods.no_streamer_link", "no streamer behind that link")["message"], "sample": [], "n": 0}]
        return [_hit(list_vods(cfg, ch["provider"], ch["key"], limit=3, timeout=timeout))]
    if link["kind"] == "unknown":
        return [{"provider": link.get("provider"), "key": None, "name": q, "url": link.get("url") or "", "ok": False,
                 "error": link.get("error") or _msg("server.vods.unusable_link", "not a link this can use")["message"], "sample": [], "n": 0}]
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(provs) + 1, thread_name_prefix="vodsearch") as ex:
        futs = [(p, ex.submit(list_vods, cfg, p, name_key(p, q), 3, False, None, timeout)) for p in provs]
        yt = ex.submit(youtube_channels, q, 5, timeout) if "YouTube" in provs else None
        rows = []
        for p, f in futs:
            try:
                rows.append(_hit(f.result()))
            except Exception as e:                       # noqa: BLE001 -- one site never fails the search
                rows.append({"provider": p, "key": name_key(p, q), "name": q, "url": "", "ok": False,
                             "error": str(e)[:200], "sample": [], "n": 0})
        extra = yt.result() if yt else []
    have = {(r["provider"], r["key"]) for r in rows if r["ok"]}
    rows += [h for h in extra if ("YouTube", h["key"]) not in have]
    logger.info("[VODBrowse] search q=%s sites=%d ok=%d secs=%.1f", q, len(provs), sum(1 for r in rows if r["ok"]),
                time.monotonic() - t0)
    return rows


def resolve(cfg, text: str, inspect: bool = True) -> dict:
    """parse_link + for a VOD link the streamer behind it (vods.inspect: channel_key / channel_url) ->
    adds channel {provider, key, name, url} (and meta = the inspect result) so the page can open them.
    inspect=False: no network at all -- the link's own answer (a VOD link's channel only when the link names it);
    for a caller that already inspected the VOD (the Import VOD dialog) and must not wait for it twice."""
    r = parse_link(text)
    if r["kind"] == "channel":
        r["channel"] = {"provider": r["provider"], "key": r["key"], "name": r["key"], "url": r["url"]}
    elif r["kind"] == "vod" and not inspect:
        if r.get("key"):
            r["channel"] = {"provider": r["provider"], "key": r["key"], "name": r["key"],
                            "url": channel_url(r["provider"], r["key"])}
    elif r["kind"] == "vod":
        try:
            meta = vods.inspect(cfg, r["url"])
            r["meta"] = meta
            if meta.get("channel_key"):
                k = norm_key(r["provider"], meta["channel_key"])
                r["channel"] = {"provider": r["provider"], "key": k, "name": meta.get("channel") or k,
                                "url": meta.get("channel_url") or channel_url(r["provider"], k)}
        except vods.VodError as e:
            r["error"] = str(e)
        if not r.get("channel") and r.get("key"):       # Kick / TikTok / Twitch clip links name the streamer
            r["channel"] = {"provider": r["provider"], "key": r["key"], "name": r["key"],
                            "url": channel_url(r["provider"], r["key"])}
    return r


# ---- favorites --------------------------------------------------------------------------------------
def _fav_row(r: dict) -> dict:
    try:
        seen = json.loads(r.get("last_seen_ids") or "[]")
    except ValueError:
        seen = []
    return {"id": r["id"], "provider": r["provider"], "key": r["channel_key"], "name": r["channel_name"] or r["channel_key"],
            "url": r["channel_url"] or channel_url(r["provider"], r["channel_key"]), "note": r["note"],
            "last_checked_utc": r["last_checked_utc"], "created_utc": r["created_utc"], "n_seen": len(seen)}


def fav_list(db) -> list[dict]:
    return [_fav_row(r) for r in db.query("SELECT * FROM vod_favorites ORDER BY provider, lower(channel_name), channel_key")]


def fav_get(db, fav_id: int) -> dict | None:
    r = db.one("SELECT * FROM vod_favorites WHERE id=?", (fav_id,))
    return _fav_row(r) if r else None


def fav_find(db, provider: str, key: str) -> dict | None:
    r = db.one("SELECT * FROM vod_favorites WHERE provider=? AND channel_key=?", (provider, norm_key(provider, key)))
    return _fav_row(r) if r else None


def fav_add(db, provider: str, key: str, name: str = "", url: str = "", note: str = "") -> tuple[dict, bool]:
    """(favorite, created). Idempotent: the same streamer twice returns the first row. VODs of a listing
    still in the cache count as seen, so the first "check for new" only counts what came after."""
    if provider not in vods.PROVIDER_NAMES:
        raise ValueError(f"unknown site '{provider}'")
    key = norm_key(provider, key)
    if not key:
        raise ValueError("no streamer key")
    have = fav_find(db, provider, key)
    if have:
        return have, False
    seen = cached_ids(provider, key, importable_only=True)
    fid = db.execute("INSERT OR IGNORE INTO vod_favorites (provider, channel_key, channel_name, channel_url, note,"
                     " last_checked_utc, last_seen_ids, created_utc) VALUES (?,?,?,?,?,?,?,?)",
                     (provider, key, (name or "").strip() or key, (url or "").strip() or channel_url(provider, key),
                      note or "", time.time() if seen is not None else None, json.dumps(seen or []), time.time()))
    # an ignored insert (the same streamer added by a parallel request since fav_find) leaves lastrowid at a
    # stale value: changes() on this thread's connection says whether THIS statement inserted the row
    if not db.scalar("SELECT changes()"):
        row = fav_find(db, provider, key)
        if row is None:
            raise RuntimeError(f"favorite {provider}/{key} was neither added nor found")
        return row, False
    logger.info("[VODBrowse] favorite added provider=%s key=%s id=%s", provider, key, fid)
    return fav_get(db, fid), True


def fav_delete(db, fav_id: int) -> bool:
    return db.execute("DELETE FROM vod_favorites WHERE id=?", (fav_id,)) > 0


def fav_update(db, fav_id: int, note: str | None = None, name: str | None = None) -> dict | None:
    if note is not None:
        db.execute("UPDATE vod_favorites SET note=? WHERE id=?", (str(note), fav_id))
    if name is not None and str(name).strip():
        db.execute("UPDATE vod_favorites SET channel_name=? WHERE id=?", (str(name).strip(), fav_id))
    return fav_get(db, fav_id)


def refresh_favorites(cfg, db, ids=None, limit: int = 15) -> list[dict]:
    """Re-list every favorite (or ids), concurrently; new_count = VODs not seen at the last check (newer than
    the newest seen one), not in the library, not queued, not live. The current ids then become last_seen; a
    failed listing keeps the old ones. Never checked and never listed = every VOD counts as new."""
    rows = db.query("SELECT * FROM vod_favorites ORDER BY provider, lower(channel_name), channel_key")
    if ids:
        want = {int(i) for i in ids}
        rows = [r for r in rows if r["id"] in want]
    if not rows:
        return []
    with ThreadPoolExecutor(max_workers=min(6, len(rows)), thread_name_prefix="vodfav") as ex:
        futs = [(r, ex.submit(list_vods, cfg, r["provider"], r["channel_key"], limit, True, None)) for r in rows]
        got = []
        for r, f in futs:
            try:
                got.append((r, f.result()))
            except Exception as e:                       # noqa: BLE001
                got.append((r, {"vods": [], "error": str(e)[:200]}))
    out, now = [], time.time()
    for r, listing in got:
        fav = _fav_row(r)
        if listing.get("error"):
            fav.update(new_count=None, error=listing["error"])
            out.append(fav)
            continue
        mark(db, r["provider"], listing["vods"])
        try:
            seen = set(json.loads(r.get("last_seen_ids") or "[]"))
        except ValueError:
            seen = set()
        # every site lists newest first: only what sits above the newest VOD seen last time is new (a favorite
        # seeded from a 5-VOD listing must not call the 10 older VODs of a 15-VOD check "new")
        # only finished (importable) VODs take part: a live stream / upcoming placeholder is neither new nor seen
        vl = [v for v in listing["vods"] if v.get("id") and v.get("importable")]
        top = next((i for i, v in enumerate(vl) if v["id"] in seen), len(vl))
        new = [v for v in vl[:top] if v["id"] not in seen and not v["imported"] and not v["queued"]]
        ids_now = _seen_ids(listing["vods"])
        name = listing["channel"].get("name")
        name = name if name and name != r["channel_key"] else r["channel_name"]
        db.execute("UPDATE vod_favorites SET last_seen_ids=?, last_checked_utc=?, channel_name=? WHERE id=?",
                   (json.dumps(ids_now), now, name, r["id"]))
        fav.update(new_count=len(new), new_ids=[v["id"] for v in new], last_checked_utc=now, n_seen=len(ids_now),
                   name=name, latest=listing["vods"][0] if listing["vods"] else None)
        out.append(fav)
    logger.info("[VODBrowse] favorites refreshed n=%d new=%d", len(out), sum(f.get("new_count") or 0 for f in out))
    return out
