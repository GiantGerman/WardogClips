"""Transcript storage and search: segments per recording + an FTS5 index over the text
(LIKE fallback when this Python's sqlite lacks FTS5)."""

import json
import logging
import re
import time

logger = logging.getLogger("wclips.transcripts")

_TOKEN = re.compile(r"[\w'\-\$]+", re.UNICODE)


def fts_query(q: str) -> str:
    """User text -> safe FTS5 query: every token quoted (so `50 cal`, `let's go`, `$1,500`
    cannot break the parser), implicit AND, the last token a prefix when it is 3+ chars."""
    toks = [t.replace('"', "") for t in _TOKEN.findall(q or "")]
    toks = [t for t in toks if t]
    if not toks:
        return ""
    parts = [f'"{t}"' for t in toks]
    if len(toks[-1]) >= 3:
        parts[-1] += "*"
    return " ".join(parts)


# ---- which recordings "Transcribe all" queues --------------------------------------------------
# missing  = never transcribed, or the last attempt failed
# outdated = missing + transcripts made before the per-track pass: those only read the single
#            "most speech" track, so the player's own mic is not in them. A current transcript has
#            stages.transcribe.v >= 2 (or, from before `v` existed, a `tracks` map).
# all      = every probed recording on disk (re-transcribes and replaces what is there)
SCOPES = ("missing", "outdated", "all")
TRANSCRIBE_VERSION = 2
_ST = "CASE WHEN json_valid(stages) THEN stages END"
_SQL_MISSING = f"COALESCE(json_extract({_ST}, '$.transcribe.state'), '') != 'done'"
_SQL_OLD = (f"(json_extract({_ST}, '$.transcribe.state') = 'done'"
            f" AND COALESCE(json_extract({_ST}, '$.transcribe.v'), 0) < {TRANSCRIBE_VERSION}"
            f" AND json_extract({_ST}, '$.transcribe.tracks') IS NULL)")


def candidates(db, scope: str = "missing") -> list[dict]:
    """Probed recordings on disk that `scope` would (re)transcribe, newest first."""
    if scope == "missing":
        where = _SQL_MISSING
    elif scope == "outdated":
        where = f"({_SQL_MISSING} OR {_SQL_OLD})"
    elif scope == "all":
        where = "1=1"
    else:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    return db.query("SELECT * FROM recordings WHERE status='probed' AND missing=0 AND " + where
                    + " ORDER BY COALESCE(name_utc, mtime) DESC")


def seconds_per_second(db) -> float | None:
    """Wall seconds a transcribe took per second of recording (median of the ones that recorded
    `secs`), for the "about N h" estimate. None until one has finished."""
    rows = db.query(f"SELECT duration_s d, json_extract({_ST}, '$.transcribe.secs') s FROM recordings"
                    f" WHERE json_extract({_ST}, '$.transcribe.secs') > 0 AND duration_s > 60")
    vals = sorted(r["s"] / r["d"] for r in rows if r["d"])
    return vals[len(vals) // 2] if vals else None


def plan(db) -> dict:
    """Counts / hours / estimate per scope for the Transcribe all dialog."""
    busy = {r["rec_id"] for r in db.query("SELECT rec_id FROM jobs WHERE stage='transcribe' AND state IN ('queued','running')")}
    rate = seconds_per_second(db)
    out = {"scopes": {}, "rate": rate, "busy": len(busy)}
    for scope in SCOPES:
        rows = candidates(db, scope)
        todo = [r for r in rows if r["id"] not in busy]
        secs = sum(float(r.get("duration_s") or 0) for r in rows)
        todo_secs = sum(float(r.get("duration_s") or 0) for r in todo)
        out["scopes"][scope] = {"n": len(rows), "hours": round(secs / 3600, 1), "already_queued": len(rows) - len(todo),
                                "to_queue": len(todo), "est_s": round(todo_secs * rate) if rate else None}
    return out


def replace_segments(db, rec_id: int, rows: list[dict], model: str) -> int:
    now = time.time()
    con = db.connect()
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("DELETE FROM transcript_segments WHERE rec_id=?", (rec_id,))
        con.executemany(
            "INSERT INTO transcript_segments (rec_id, t0, t1, text, words, avg_logprob, no_speech_prob, model, track,"
            " speaker, created_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(rec_id, r["t0"], r["t1"], r["text"], json.dumps(r.get("words") or []), r.get("avg_logprob"),
              r.get("no_speech_prob"), model, r.get("track"), r.get("speaker") or "", now)
             for r in sorted(rows, key=lambda r: r["t0"])])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return len(rows)


def segments(db, rec_id: int, with_words: bool = False) -> list[dict]:
    cols = "id, rec_id, t0, t1, text, avg_logprob, no_speech_prob, model, track, speaker" + (", words" if with_words else "")
    rows = db.query(f"SELECT {cols} FROM transcript_segments WHERE rec_id=? ORDER BY t0", (rec_id,))
    if with_words:
        for r in rows:
            try:
                r["words"] = json.loads(r["words"] or "[]")
            except json.JSONDecodeError:
                r["words"] = []
    return rows


def summary(db, rec_id: int) -> dict:
    r = db.one("SELECT COUNT(*) n, COALESCE(SUM(t1 - t0), 0) speech_s, MAX(model) model, MAX(created_utc) utc"
               " FROM transcript_segments WHERE rec_id=?", (rec_id,)) or {}
    return {"n": r.get("n", 0) or 0, "speech_s": round(r.get("speech_s") or 0.0, 1), "model": r.get("model"),
            "utc": r.get("utc")}


def search(db, q: str, limit: int = 50, drive: str = "") -> dict:
    """Hits across every recording, best match first (bm25), snippet with [brackets] round
    the matches. Falls back to LIKE when FTS5 is unavailable."""
    limit = max(1, min(int(limit), 500))
    q = (q or "").strip()
    if len(q) < 2:
        return {"q": q, "hits": [], "fts": bool(db.fts)}
    where_drive = " AND r.drive=?" if drive else ""
    params_drive = (drive.upper(),) if drive else ()
    if db.fts:
        fq = fts_query(q)
        if not fq:
            return {"q": q, "hits": [], "fts": True}
        try:
            rows = db.query(
                "SELECT s.id seg_id, s.rec_id, s.t0, s.t1, s.text, s.speaker, r.name rec_name, r.drive,"
                " snippet(transcript_fts, 0, '[', ']', '...', 14) snippet, bm25(transcript_fts) rank"
                " FROM transcript_fts JOIN transcript_segments s ON s.id = transcript_fts.rowid"
                " JOIN recordings r ON r.id = s.rec_id"
                f" WHERE transcript_fts MATCH ?{where_drive} ORDER BY rank LIMIT ?", (fq, *params_drive, limit))
            return {"q": q, "hits": rows, "fts": True}
        except Exception as e:  # noqa: BLE001 - a bad FTS expression must not 500
            logger.warning("[Search] fts failed q=%r err=%s -- LIKE fallback", q, e)
    like = f"%{q}%"
    rows = db.query(
        "SELECT s.id seg_id, s.rec_id, s.t0, s.t1, s.text, s.speaker, r.name rec_name, r.drive, s.text snippet, 0 rank"
        " FROM transcript_segments s JOIN recordings r ON r.id = s.rec_id"
        f" WHERE s.text LIKE ?{where_drive} ORDER BY s.rec_id DESC, s.t0 LIMIT ?", (like, *params_drive, limit))
    return {"q": q, "hits": rows, "fts": False}
