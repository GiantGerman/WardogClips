"""Detection zones per streamer: where the reward ticker, the kill feed, the money readout and the
respawn text sit on THEIR stream, drawn on a sample frame in the zones editor and stored
as versioned templates in `zone_templates`.

Why: the built-in HUD boxes (vision/hud.py) were measured on 3440x1440 OBS footage.
A streamer's VOD is 16:9 (or a 1080x1920 TikTok), often with a facecam or overlay where the kill
feed would be, and the layout moves when they rework the stream -- so a streamer gets their own
boxes, and a new VERSION (dated by `effective_from`) when the layout changes; older VODs keep the
version that was current when they were streamed.

Boxes are stored NORMALIZED (0..1 of the frame, [x, y, w, h]) so a template drawn on a 1080p frame
applies to the same streamer's 1440p VOD. `geometry()` turns them into pixel boxes for a frame
size: even sizes and offsets inside the frame, because the keyframe pass pads + vstacks 4:2:0
crops and ffmpeg refuses an odd width (see hud.rois). A template drawn on one aspect ratio and
applied to another stretches with the frame; streamers keep their aspect, so that is accepted.

With no template a recording is detected exactly as before (vision/detect.py keeps the hud path).

Which template applies (`resolve`): one pinned to the recording, else the streamer's version with
the largest effective_from <= the recording's start (the earliest version for an older one), else a
provider-wide template (channel_key '*'), else none ('default'). OBS recordings are provider 'OBS',
channel_key '*'.

Export / import is a JSON file (optionally with the sample frames as base64) so templates move
between machines; identity on import = (provider, channel_key, effective_from).
"""

import base64
import json
import logging
import math
import time
from pathlib import Path

from core.messages import MsgError, msg
from utils import paths
from vision import hud

logger = logging.getLogger("wclips.zones")

FORMAT = "wardogclips-zone-templates"
VERSION = 1
OBS = "OBS"
ANY = "*"
OBS_NAME = "My OBS recordings"
BOX_KEYS = ("ticker", "feed", "money", "center")
REQUIRED = ("ticker", "feed")
MIN_SIDE = 0.005                  # a box narrower / shorter than this (normalized) is a mis-click
EPS = 1e-4                        # rounding slack for "inside the frame / inside the ticker"
MAX_SAMPLE_BYTES = 25 * 1024 * 1024
# built-in Season 2 proportions, used when a template leaves the optional zones out
MONEY_OF_TICKER = 70 / 160        # money readout band = the top 70 px of the 160 px ticker crop
ROW_OF_FEED = 26 / 300            # one kill-feed row = 26 px of the 300 px feed crop


class NotFound(LookupError):
    pass


def zones_dir() -> Path:
    """<DATA_DIR>/zones (evaluated per call: tests point DATA_DIR at a temp dir)."""
    d = paths.DATA_DIR / "zones"
    d.mkdir(parents=True, exist_ok=True)
    return d


def canon_provider(p: str) -> str:
    """'twitch' -> 'Twitch' (file names and the vods module use the capitalized site name)."""
    from media.vods import PROVIDER_NAMES
    s = str(p or "").strip()
    for name in (*PROVIDER_NAMES, OBS):
        if s.lower() == name.lower():
            return name
    return s


def canon_key(k: str) -> str:
    """Lower case except a YouTube 'channel/<id>' (case-sensitive): vods.canon_channel_key."""
    from media.vods import canon_channel_key
    return canon_channel_key(k) if str(k or "").strip() != ANY else ANY


# ---- defaults / validation / geometry -------------------------------------------------------
def _norm(roi: hud.Roi, width: int, height: int) -> list[float]:
    return [round(roi.x / width, 6), round(roi.y / height, 6), round(roi.w / width, 6), round(roi.h / height, 6)]


def _center_box(width: int, height: int) -> list[float]:
    """The 1400x440-at-1440p respawn box scaled with the height, centered, normalized."""
    cw0, ch0 = hud.CENTER_BASE
    s = height / hud.BASE_H
    cw, ch = min(width, cw0 * s), min(height, ch0 * s)
    return [round((width - cw) / 2 / width, 6), round((height - ch) / 2 / height, 6),
            round(cw / width, 6), round(ch / height, 6)]


def default_zones(width: int, height: int) -> dict:
    """The built-in profile (hud.rois / money_y / feed_row_h + the respawn box) as normalized zones."""
    width, height = int(width), int(height)
    r = hud.rois(width, height)
    tk = r["ticker"]
    my = min(tk.h, hud.money_y(height))
    return {"ticker": _norm(tk, width, height), "feed": _norm(r["feed"], width, height),
            "money": [round(tk.x / width, 6), round(tk.y / height, 6), round(tk.w / width, 6), round(my / height, 6)],
            "center": _center_box(width, height),
            "feed_row_h": round(hud.feed_row_h(height) / height, 6)}


def _num(v, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise MsgError(msg("server.zones.not_number", "{field}: not a number.", field=what))
    v = float(v)
    if not math.isfinite(v):
        raise MsgError(msg("server.zones.not_number", "{field}: not a number.", field=what))
    return v


def _box(v, name: str) -> list[float]:
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        raise MsgError(msg("server.zones.bad_box", "{zone}: expected a box [x, y, w, h].", zone=name))
    x, y, w, h = (_num(c, name) for c in v)
    if w <= MIN_SIDE or h <= MIN_SIDE:
        raise MsgError(msg("server.zones.too_small", "{zone} is too small: its width and height must be more than {min} of the frame.", zone=name, min=MIN_SIDE))
    if x < -EPS or y < -EPS or x + w > 1 + EPS or y + h > 1 + EPS:
        raise MsgError(msg("server.zones.outside", "{zone} lies outside the frame.", zone=name))
    x, y = max(0.0, x), max(0.0, y)
    w, h = min(w, 1.0 - x), min(h, 1.0 - y)
    return [round(x, 6), round(y, 6), round(w, 6), round(h, 6)]


def validate(zones) -> dict:
    """Cleaned zones {ticker, feed, money|None, center|None, feed_row_h|None}, or ValueError."""
    if isinstance(zones, str):
        try:
            zones = json.loads(zones)
        except ValueError:
            raise MsgError(msg("server.zones.not_json", "The zones are not valid JSON."))
    if not isinstance(zones, dict):
        raise MsgError(msg("server.zones.not_object", "The zones must be an object."))
    out: dict = {}
    for name in BOX_KEYS:
        v = zones.get(name)
        if v is None:
            if name in REQUIRED:
                raise MsgError(msg("server.zones.required", "The {zone} zone is required.", zone=name))
            out[name] = None
        else:
            out[name] = _box(v, name)
    m, t = out["money"], out["ticker"]
    if m is not None and (m[0] < t[0] - EPS or m[1] < t[1] - EPS or m[0] + m[2] > t[0] + t[2] + EPS
                          or m[1] + m[3] > t[1] + t[3] + EPS):
        raise MsgError(msg("server.zones.money_inside", "The money zone must lie inside the ticker zone."))
    rh = zones.get("feed_row_h")
    if rh is None:
        out["feed_row_h"] = None
    else:
        rh = _num(rh, "feed_row_h")
        if rh <= 0 or rh > out["feed"][3] + EPS:
            raise MsgError(msg("server.zones.row_height", "The kill feed row height must be more than 0 and no taller than the kill feed zone."))
        out["feed_row_h"] = round(rh, 6)
    return out


def _even_floor(v: float) -> int:
    return int(v) // 2 * 2


def _px(name: str, box: list[float], width: int, height: int) -> hud.Roi:
    """A normalized box as an even-sized, even-offset pixel Roi inside the frame. Edges are rounded
    (not x and w separately) so a box drawn to the frame edge stays flush with it."""
    x, y, w, h = box
    x0 = min(_even_floor(round(x * width)), max(0, _even_floor(width - 2)))
    y0 = min(_even_floor(round(y * height)), max(0, _even_floor(height - 2)))
    x1, y1 = min(round((x + w) * width), width), min(round((y + h) * height), height)
    pw = min(max(2, _even_floor(x1 - x0)), max(2, _even_floor(width - x0)))
    ph = min(max(2, _even_floor(y1 - y0)), max(2, _even_floor(height - y0)))
    return hud.Roi(name, x0, y0, pw, ph)


def geometry(zones, width: int, height: int) -> dict:
    """Pixel geometry for a frame: {rois: {ticker, feed}, money_y (px inside the ticker crop),
    feed_row_h (px), center (Roi)}; optional zones fall back to the Season 2 proportions."""
    z = validate(zones)
    width, height = int(width), int(height)
    tk = _px("ticker", z["ticker"], width, height)
    fd = _px("feed", z["feed"], width, height)
    if z["money"] is not None:
        my = int(round((z["money"][1] + z["money"][3]) * height)) - tk.y
    else:
        my = int(round(tk.h * MONEY_OF_TICKER))
    my = max(0, min(tk.h, my))
    if z["feed_row_h"] is not None:
        row_h = int(round(z["feed_row_h"] * height))
    else:
        row_h = int(round(fd.h * ROW_OF_FEED))
    row_h = max(6, row_h)
    center = _px("center", z["center"] if z["center"] is not None else _center_box(width, height), width, height)
    return {"rois": {"ticker": tk, "feed": fd}, "money_y": my, "feed_row_h": row_h, "center": center}


def geometry_json(geo: dict) -> dict:
    """geometry() for the page: plain dicts instead of Roi."""
    box = lambda r: {"x": r.x, "y": r.y, "w": r.w, "h": r.h}   # noqa: E731
    return {"ticker": box(geo["rois"]["ticker"]), "feed": box(geo["rois"]["feed"]), "center": box(geo["center"]),
            "money_y": geo["money_y"], "feed_row_h": geo["feed_row_h"]}


# ---- rows ------------------------------------------------------------------------------------
def to_dict(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    try:
        out["zones"] = json.loads(out.get("zones") or "{}")
    except ValueError:
        out["zones"] = {}
    return out


def get(db, tpl_id: int) -> dict | None:
    return to_dict(db.one("SELECT * FROM zone_templates WHERE id=?", (int(tpl_id),)))


def list_templates(db, provider: str | None = None, channel_key: str | None = None) -> list[dict]:
    where, params = [], []
    if provider:
        where.append("provider=?")
        params.append(canon_provider(provider))
    if channel_key:
        where.append("channel_key=?")
        params.append(canon_key(channel_key))
    sql = "SELECT * FROM zone_templates" + (" WHERE " + " AND ".join(where) if where else "")
    return [to_dict(r) for r in db.query(sql + " ORDER BY effective_from DESC, id DESC", params)]


def _identity_taken(db, provider: str, channel_key: str, effective_from: float, not_id: int | None = None) -> bool:
    return db.one("SELECT id FROM zone_templates WHERE provider=? AND channel_key=? AND ABS(effective_from - ?) < 0.5"
                  " AND id IS NOT ?", (provider, channel_key, float(effective_from), not_id)) is not None


def create(db, provider: str, channel_key: str, zones, channel_name: str = "", name: str = "",
           effective_from: float = 0.0, note: str = "", sample_w: int | None = None, sample_h: int | None = None,
           sample_rec_id: int | None = None, sample_t: float | None = None, sample_jpg: str | None = None) -> dict:
    provider, channel_key = canon_provider(provider), canon_key(channel_key)
    if not provider or not channel_key:
        raise MsgError(msg("server.zones.streamer_required", "Pick a site and a streamer."))
    z = validate(zones)
    eff = _num(effective_from or 0, "effective_from")
    if _identity_taken(db, provider, channel_key, eff):
        raise MsgError(msg("server.zones.date_taken_edit", "This streamer already has a version from that date. Edit it or pick another date."))
    now = time.time()
    tid = db.execute(
        "INSERT INTO zone_templates (provider, channel_key, channel_name, name, effective_from, zones, sample_w, sample_h,"
        " sample_rec_id, sample_t, sample_jpg, note, created_utc, updated_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (provider, channel_key, str(channel_name or "")[:200], str(name or "")[:200], eff, json.dumps(z),
         _int_or_none(sample_w), _int_or_none(sample_h), _int_or_none(sample_rec_id),
         None if sample_t is None else float(sample_t), sample_jpg, str(note or "")[:2000], now, now))
    logger.info("[Zones] template created id=%s provider=%s key=%s from=%.0f", tid, provider, channel_key, eff)
    return get(db, tid)


def _int_or_none(v) -> int | None:
    try:
        return None if v is None or v == "" else int(v)
    except (TypeError, ValueError):
        return None


_EDITABLE = ("name", "channel_name", "note", "effective_from", "zones", "sample_w", "sample_h", "sample_rec_id",
             "sample_t", "sample_jpg")


def update(db, tpl_id: int, **fields) -> dict:
    """Partial update of the editable fields (others are ignored)."""
    cur = get(db, tpl_id)
    if cur is None:
        raise NotFound(msg("server.zones.no_template", "That zone template does not exist anymore.")["message"])
    sets, params = [], []
    for k in _EDITABLE:
        if k not in fields:
            continue
        v = fields[k]
        if k == "zones":
            v = json.dumps(validate(v))
        elif k == "effective_from":
            v = _num(v or 0, "effective_from")
            if _identity_taken(db, cur["provider"], cur["channel_key"], v, not_id=cur["id"]):
                raise MsgError(msg("server.zones.date_taken", "This streamer already has a version from that date."))
        elif k in ("sample_w", "sample_h", "sample_rec_id"):
            v = _int_or_none(v)
        elif k == "sample_t":
            v = None if v is None else float(v)
        elif k in ("name", "channel_name"):
            v = str(v or "")[:200]
        elif k == "note":
            v = str(v or "")[:2000]
        sets.append(f"{k}=?")
        params.append(v)
    if sets:
        db.execute(f"UPDATE zone_templates SET {', '.join(sets)}, updated_utc=? WHERE id=?",
                   (*params, time.time(), cur["id"]))
        if "sample_jpg" in fields and cur.get("sample_jpg") and cur["sample_jpg"] != fields["sample_jpg"]:
            _drop_sample(db, cur["sample_jpg"])
    return get(db, tpl_id)


def delete(db, tpl_id: int) -> int:
    """Delete a template, unpin recordings pinned to it, remove its sample frame. -> recordings unpinned."""
    cur = get(db, tpl_id)
    if cur is None:
        raise NotFound(msg("server.zones.no_template", "That zone template does not exist anymore.")["message"])
    n = db.execute("UPDATE recordings SET zone_template_id=NULL WHERE zone_template_id=?", (cur["id"],))
    db.execute("DELETE FROM zone_templates WHERE id=?", (cur["id"],))
    if cur.get("sample_jpg"):
        _drop_sample(db, cur["sample_jpg"])
    logger.info("[Zones] template deleted id=%s unpinned=%d", cur["id"], n)
    return n


def new_version(db, tpl_id: int, effective_from: float, sample_jpg: str | None = None, sample_w: int | None = None,
                sample_h: int | None = None, sample_rec_id: int | None = None, sample_t: float | None = None,
                **overrides) -> dict:
    """A copy of a template as a new dated version (same streamer). The sample frame is copied too
    (its own file) unless a new one is given; overrides = name / zones / note."""
    cur = get(db, tpl_id)
    if cur is None:
        raise NotFound(msg("server.zones.no_template", "That zone template does not exist anymore.")["message"])
    if sample_jpg is None and cur.get("sample_jpg"):
        sample_jpg = _copy_sample(cur["sample_jpg"])
        sample_w, sample_h = cur.get("sample_w"), cur.get("sample_h")
        sample_rec_id, sample_t = cur.get("sample_rec_id"), cur.get("sample_t")
    try:
        return create(db, cur["provider"], cur["channel_key"], overrides.get("zones") or cur["zones"],
                      channel_name=cur["channel_name"], name=overrides.get("name", cur["name"]),
                      effective_from=effective_from, note=overrides.get("note", cur["note"]),
                      sample_w=sample_w, sample_h=sample_h, sample_rec_id=sample_rec_id, sample_t=sample_t,
                      sample_jpg=sample_jpg)
    except ValueError:
        if sample_jpg and sample_jpg != cur.get("sample_jpg"):
            _drop_sample(db, sample_jpg)
        raise


# ---- which template applies ------------------------------------------------------------------
def rec_start(rec: dict) -> float | None:
    """When a recording's footage starts: a VOD / plain recording's name time; a replay's name time
    is when the buffer was SAVED, so start = that - duration."""
    t = rec.get("name_utc")
    dur = float(rec.get("duration_s") or 0)
    if t is None:
        m = rec.get("mtime")
        return float(m) - dur if m else None
    return float(t) - dur if rec.get("kind") == "replay" else float(t)


def streamer_of(rec: dict) -> tuple[str, str | None]:
    """(provider, channel_key) of a recording: VODs by their file name + sidecar key, everything
    else is local OBS footage (key '*')."""
    from core.library import vod_provider
    prov = vod_provider(rec.get("name") or "")
    if prov is None:
        return OBS, ANY
    return canon_provider(prov), canon_key(rec.get("channel_key")) or None


def _pick_version(rows: list[dict], start: float | None) -> dict | None:
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: (float(r["effective_from"] or 0), r["id"]))
    if start is None:
        return rows[-1]
    fit = [r for r in rows if float(r["effective_from"] or 0) <= start]
    return fit[-1] if fit else rows[0]


def resolve(db, rec: dict) -> tuple[dict | None, str]:
    """(template | None, source): 'pinned' | 'streamer' | 'provider' (a channel_key '*' template of
    the VOD's site) | 'default'. An OBS recording's own templates (OBS, '*') count as 'streamer'."""
    pin = rec.get("zone_template_id")
    if pin:
        t = get(db, pin)
        if t is not None:
            return t, "pinned"
        logger.warning("[Zones] rec=%s pinned to a missing template id=%s", rec.get("id"), pin)
    provider, key = streamer_of(rec)
    start = rec_start(rec)
    if key:
        t = _pick_version([to_dict(r) for r in db.query(
            "SELECT * FROM zone_templates WHERE provider=? AND channel_key=?", (provider, key))], start)
        if t is not None:
            return t, "streamer"
    if key != ANY:
        t = _pick_version([to_dict(r) for r in db.query(
            "SELECT * FROM zone_templates WHERE provider=? AND channel_key=?", (provider, ANY))], start)
        if t is not None:
            return t, "provider"
    return None, "default"


def pin(db, rec_id: int, tpl_id: int | None) -> None:
    if tpl_id is not None and get(db, tpl_id) is None:
        raise NotFound(msg("server.zones.no_template", "That zone template does not exist anymore.")["message"])
    if db.one("SELECT id FROM recordings WHERE id=?", (int(rec_id),)) is None:
        raise NotFound(msg("server.no_recording", "That recording does not exist anymore.")["message"])
    db.execute("UPDATE recordings SET zone_template_id=?, updated_utc=? WHERE id=?",
               (None if tpl_id is None else int(tpl_id), time.time(), int(rec_id)))
    logger.info("[Zones] rec=%s pinned template=%s", rec_id, tpl_id)


# ---- sample frames ---------------------------------------------------------------------------
def _jpeg_size(data: bytes) -> tuple[int | None, int | None]:
    """(width, height) from a JPEG's SOF header -- no decode of a 3440x1440 frame needed."""
    i = 2
    n = len(data)
    if n < 4 or data[:2] != b"\xff\xd8":
        return None, None
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            i += 1 if marker == 0xFF else 2
            continue
        seg = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + seg
    return None, None


def _write_sample(tpl_id: int, data: bytes) -> str:
    d = zones_dir()
    stamp = int(time.time() * 1000)
    while (d / f"tpl{tpl_id}_{stamp}.jpg").exists():
        stamp += 1
    name = f"tpl{tpl_id}_{stamp}.jpg"
    (d / name).write_bytes(data)
    return name


def _copy_sample(name: str) -> str | None:
    src = sample_path(name)
    if src is None:
        return None
    d = zones_dir()
    stamp = int(time.time() * 1000)
    while (d / f"copy_{stamp}.jpg").exists():
        stamp += 1
    dst = d / f"copy_{stamp}.jpg"
    dst.write_bytes(src.read_bytes())
    return dst.name


def sample_path(name: str | None) -> Path | None:
    """The sample file of a template, only when it exists and is a plain name under the zones dir."""
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    p = zones_dir() / name
    return p if p.is_file() else None


def _drop_sample(db, name: str) -> None:
    if db.one("SELECT id FROM zone_templates WHERE sample_jpg=?", (name,)) is not None:
        return                                   # still in use by another version
    p = sample_path(name)
    if p is not None:
        try:
            p.unlink()
        except OSError as e:
            logger.warning("[Zones] could not remove sample %s err=%s", name, e)


def save_sample(path: str, t: float, tpl_id: int) -> tuple[str, int | None, int | None]:
    """Grab the full-resolution frame at t of a recording (read-only) into <DATA_DIR>/zones/.
    -> (file name, width, height). Raises vision.frames.FrameError."""
    from vision import frames
    data = frames.grab_frame(path, max(0.0, float(t or 0)), max_w=16384, quality=3)
    w, h = _jpeg_size(data)
    name = _write_sample(tpl_id, data)
    logger.info("[Zones] sample grabbed tpl=%s t=%.2f size=%sx%s bytes=%d", tpl_id, float(t or 0), w, h, len(data))
    return name, w, h


def set_sample(db, tpl_id: int, rec: dict, t: float) -> dict:
    """Grab a new sample frame for a template from a recording and store it (the old file goes)."""
    name, w, h = save_sample(rec["path"], t, tpl_id)
    return update(db, tpl_id, sample_jpg=name, sample_w=w or rec.get("width"), sample_h=h or rec.get("height"),
                  sample_rec_id=rec["id"], sample_t=float(t or 0))


# ---- export / import -------------------------------------------------------------------------
_EXPORT_FIELDS = ("provider", "channel_key", "channel_name", "name", "effective_from", "zones", "sample_w", "sample_h",
                  "note")


def export(db, ids: list[int] | None = None, include_samples: bool = True) -> dict:
    if ids:
        marks = ",".join("?" for _ in ids)
        rows = db.query(f"SELECT * FROM zone_templates WHERE id IN ({marks})", tuple(int(i) for i in ids))
    else:
        rows = db.query("SELECT * FROM zone_templates")
    rows = sorted((to_dict(r) for r in rows), key=lambda r: (r["provider"], r["channel_key"], r["effective_from"]))
    out = []
    for r in rows:
        item = {k: r.get(k) for k in _EXPORT_FIELDS}
        if include_samples:
            p = sample_path(r.get("sample_jpg"))
            if p is not None:
                item["sample_jpg_b64"] = base64.b64encode(p.read_bytes()).decode("ascii")
        out.append(item)
    logger.info("[Zones] export templates=%d samples=%s", len(out), include_samples)
    return {"format": FORMAT, "version": VERSION, "exported_utc": time.time(), "templates": out}


def _sample_from_b64(s) -> bytes | None:
    if not s:
        return None
    if not isinstance(s, str) or len(s) > MAX_SAMPLE_BYTES * 4 // 3 + 8:
        raise MsgError(msg("server.zones.sample_bad", "The sample image is too large or not valid."))
    try:
        data = base64.b64decode(s, validate=True)
    except (ValueError, TypeError):
        raise MsgError(msg("server.zones.sample_bad", "The sample image is too large or not valid."))
    if data[:2] != b"\xff\xd8":
        raise MsgError(msg("server.zones.sample_not_jpeg", "The sample image is not a JPEG."))
    return data


def import_(db, data, overwrite: bool = False) -> dict:
    """Templates from an export file. Existing (provider, channel_key, effective_from) versions are
    skipped, or replaced with overwrite. -> {added, updated, skipped, errors (count), messages}.
    A bad file (format / version) raises ValueError; a bad template is counted and the rest go on."""
    if isinstance(data, (str, bytes)):
        try:
            data = json.loads(data)
        except ValueError:
            raise MsgError(msg("server.zones.file_not_json", "This is not a JSON file."))
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise MsgError(msg("server.zones.file_wrong", "This is not a WardogClips zone template file."))
    if data.get("version") != VERSION:
        raise MsgError(msg("server.zones.file_version", "This template file has version {version}; this version of WardogClips reads version {supported}.", version=str(data.get("version")), supported=VERSION))
    items = data.get("templates")
    if not isinstance(items, list):
        raise MsgError(msg("server.zones.file_empty", "The file has no list of templates."))
    res = {"added": 0, "updated": 0, "skipped": 0, "errors": 0, "messages": []}
    for i, it in enumerate(items):
        try:
            if not isinstance(it, dict):
                raise MsgError(msg("server.zones.item_not_object", "This entry is not a template."))
            provider, key = canon_provider(it.get("provider")), canon_key(it.get("channel_key"))
            if not provider or not key:
                raise MsgError(msg("server.zones.streamer_required", "Pick a site and a streamer."))
            zones = validate(it.get("zones"))
            eff = _num(it.get("effective_from") or 0, "effective_from")
            jpg = _sample_from_b64(it.get("sample_jpg_b64"))
            fields = {"channel_name": it.get("channel_name") or "", "name": it.get("name") or "",
                      "note": it.get("note") or "", "sample_w": it.get("sample_w"), "sample_h": it.get("sample_h")}
            old = db.one("SELECT id, sample_jpg FROM zone_templates WHERE provider=? AND channel_key=?"
                         " AND ABS(effective_from - ?) < 0.5", (provider, key, eff))
            if old is not None and not overwrite:
                res["skipped"] += 1
                continue
            if old is not None:
                # sample_w/h describe the template's sample frame: an item without an image leaves the
                # local frame (file, size, rec, t) alone; with no local frame its size is taken if given
                upd = {k: v for k, v in fields.items() if k not in ("sample_w", "sample_h")}
                if jpg is None and sample_path(old.get("sample_jpg")) is None:
                    upd.update({k: fields[k] for k in ("sample_w", "sample_h") if fields[k] is not None})
                t = update(db, old["id"], zones=zones, **upd)
                res["updated"] += 1
            else:
                t = create(db, provider, key, zones, effective_from=eff, **fields)
                res["added"] += 1
            if jpg is not None:
                if fields["sample_w"] is None or fields["sample_h"] is None:
                    w, h = _jpeg_size(jpg)
                    fields["sample_w"], fields["sample_h"] = w, h
                update(db, t["id"], sample_jpg=_write_sample(t["id"], jpg), sample_w=fields["sample_w"],
                       sample_h=fields["sample_h"], sample_rec_id=None, sample_t=None)
        except ValueError as e:
            res["errors"] += 1
            label = f"{(it or {}).get('provider', '?')} {(it or {}).get('channel_key', '?')}" if isinstance(it, dict) else "?"
            m = msg("server.zones.import_item", "Template {n} ({label}): {error}", n=i + 1, label=label, error=str(e))
            res["messages"].append(m["message"])
            res.setdefault("messages_i18n", []).append({"code": m["code"], "params": m["params"],
                                                        "error_i18n": getattr(e, "i18n", None)})
    logger.info("[Zones] import added=%d updated=%d skipped=%d errors=%d overwrite=%s", res["added"], res["updated"],
                res["skipped"], res["errors"], overwrite)
    return res
