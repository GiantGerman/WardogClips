"""Clip search: the left panel's upper cell. Events across the whole library, filtered by kind,
status, score, player name, kill distance, kill streak, words said nearby (transcript), date,
recording type and drive. One SQL query; `params` come straight from the query string."""

import logging

from core.messages import MsgError, msg

logger = logging.getLogger("wclips.clipsearch")

KINDS = ("kill", "assist", "death", "spotted", "revive", "squad", "voice", "manual")
STATUSES = ("notfalse", "new", "kept", "false", "any")
REC_KINDS = ("any", "replay", "recording", "vod")
ORDERS = {"score": "e.score DESC, abs_utc DESC", "newest": "abs_utc DESC, e.score DESC", "oldest": "abs_utc ASC, e.score DESC"}
MAX_SAID_WORDS = 16                  # each word is one EXISTS; SQLite's expression depth is finite
SEG_MAX_S = 60.0                     # no transcript line is longer (whisper windows are 30 s)
# a transcript line overlapping [t_s - w, t_s + w]; params: w + SEG_MAX_S, w, w
SAID_SQL = ("FROM transcript_segments s WHERE s.rec_id = e.rec_id AND s.t0 >= e.t_s - ? AND s.t1 >= e.t_s - ?"
            " AND s.t0 <= e.t_s + ?")

_SRC = "(CASE WHEN json_valid(e.source) THEN e.source END)"
# when the recording started: a replay's name stamp is when the buffer was SAVED (its end)
_START = ("COALESCE(CASE WHEN r.kind='replay' AND r.duration_s IS NOT NULL THEN r.name_utc - r.duration_s"
          " ELSE r.name_utc END, r.mtime - COALESCE(r.duration_s, 0))")


def _like(word: str) -> str:
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _num(params: dict, key: str, lo: float | None = None, hi: float | None = None) -> float | None:
    raw = params.get(key)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise MsgError(msg("server.search.not_number", "{field} must be a number.", field=key))
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def build(params: dict, multikill_s: float = 10.0) -> tuple[str, list, dict]:
    """-> (WHERE sql, its params, the normalized filter). Raises ValueError on a bad value."""
    where, args = [], []
    norm = {}

    kinds = [k for k in str(params.get("kinds") or "").split(",") if k]
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        raise MsgError(msg("server.bad_value", "This value is not valid: {field}", field=", ".join(bad)))
    if kinds:
        where.append("e.kind IN (%s)" % ",".join("?" * len(kinds)))
        args += kinds
    norm["kinds"] = kinds

    status = str(params.get("status") or "notfalse")
    if status not in STATUSES:
        raise MsgError(msg("server.bad_value", "This value is not valid: {field}", field=status))
    if status == "notfalse":
        where.append("e.status != 'false'")
    elif status != "any":
        where.append("e.status = ?")
        args.append(status)
    norm["status"] = status

    drive = str(params.get("drive") or "").strip().upper()
    if drive:
        where.append("r.drive = ?")
        args.append(drive)

    min_score = _num(params, "min_score", 0)
    if min_score:
        where.append("e.score >= ?")
        args.append(min_score)

    player = str(params.get("player") or "").strip()
    if player:
        where.append("(e.victim LIKE ? ESCAPE '\\' OR e.killer LIKE ? ESCAPE '\\')")
        args += [_like(player), _like(player)]

    min_dist = _num(params, "min_dist", 0)
    if min_dist:
        where.append(f"CAST(json_extract({_SRC}, '$.distance_m') AS REAL) >= ?")
        args.append(min_dist)

    streak = _num(params, "streak", 0, 20)
    if streak and streak >= 2:
        w = float(multikill_s)
        where.append("e.kind = 'kill' AND (SELECT COUNT(*) FROM events k WHERE k.rec_id = e.rec_id AND k.kind = 'kill'"
                     " AND k.status != 'false' AND k.t_s BETWEEN e.t_s - ? AND e.t_s + ?) >= ?")
        args += [w, w, int(streak)]
        norm["streak_window_s"] = w

    said = [w for w in str(params.get("said") or "").split() if len(w) >= 2]
    if len(said) > MAX_SAID_WORDS:
        raise MsgError(msg("server.search.said_words", "Search for at most {max} words of speech.", max=MAX_SAID_WORDS))
    said_w = _num(params, "said_window", 1, 120) or 10.0
    said_sql = ""
    if said:
        # every word somewhere near the event -- each in ANY line of the window (whisper splits lines, and
        # the player's mic and the squad are separate lines). t0 is bounded from below too, so the (rec_id, t0)
        # index scans only the window instead of the recording up to the event.
        said_sql = SAID_SQL
        for w in said:
            where.append(f"EXISTS (SELECT 1 {said_sql} AND s.text LIKE ? ESCAPE '\\')")
            args += [said_w + SEG_MAX_S, said_w, said_w, _like(w)]
        norm.update(said=said, said_window=said_w)

    d_from, d_to = _num(params, "from"), _num(params, "to")
    if d_from is not None:
        where.append(f"({_START} + e.t_s) >= ?")
        args.append(d_from)
    if d_to is not None:
        where.append(f"({_START} + e.t_s) < ?")
        args.append(d_to)

    rec_kind = str(params.get("rec_kind") or "any")
    if rec_kind not in REC_KINDS:
        raise MsgError(msg("server.bad_value", "This value is not valid: {field}", field=rec_kind))
    if rec_kind != "any":
        where.append("r.kind = ?")
        args.append(rec_kind)

    norm["said_sql"] = said_sql
    return (" AND ".join(where) or "1=1"), args, norm


def search(db, params: dict, multikill_s: float = 10.0) -> dict:
    where, args, norm = build(params, multikill_s)
    order = str(params.get("order") or "score")
    if order not in ORDERS:
        raise MsgError(msg("server.bad_value", "This value is not valid: {field}", field=order))
    limit = int(_num(params, "limit", 1, 1000) or 300)
    base = "FROM events e JOIN recordings r ON r.id = e.rec_id WHERE " + where
    total = db.scalar("SELECT COUNT(*) " + base, args) or 0
    rows = db.query(f"SELECT e.*, r.name rec_name, r.drive, r.kind rec_kind, r.name_utc, r.duration_s, r.missing rec_missing,"
                    f" ({_START} + e.t_s) abs_utc " + base + f" ORDER BY {ORDERS[order]} LIMIT ?", [*args, limit])
    if norm["said_sql"]:
        # the line shown under the result: in the window, holding the most of the words, closest to the
        # event. SQLite can't see the outer event inside a scalar subquery's ORDER BY, so: one indexed
        # lookup per shown row (same window as the WHERE)
        said = norm["said"]
        hits = " + ".join("(text LIKE ? ESCAPE '\\')" for _ in said)
        w = norm["said_window"]
        for row in rows:
            t = float(row["t_s"])
            hit = db.one(f"SELECT text, ({hits}) n FROM transcript_segments WHERE rec_id=? AND t0 >= ? AND t1 >= ?"
                         " AND t0 <= ? ORDER BY n DESC, ABS(t0 - ?) LIMIT 1",
                         (*[_like(x) for x in said], row["rec_id"], t - w - SEG_MAX_S, t - w, t + w, t))
            row["said_text"] = hit["text"] if hit and hit["n"] else None
    return {"events": rows, "total": int(total), "limit": limit, "order": order,
            "filter": {k: v for k, v in norm.items() if k not in ("said_sql", "said_args")}}
