"""Transcript storage and search: segments per recording + an FTS5 index over the text
(LIKE fallback when this Python's sqlite lacks FTS5)."""

import json
import logging
import re
import time
import unicodedata

from core.messages import MsgError, msg

logger = logging.getLogger("wclips.transcripts")

_TOKEN = re.compile(r"[\w'\-\$]+", re.UNICODE)


# The index's remove_diacritics folds Latin letters only ("esta" finds "está"); Greek keeps its accent (tonos), so
# "καλημερα" or an all-caps "ΚΑΛΗΜΕΡΑ" would miss "Καλημέρα". A Greek word has one accented vowel: the query tries
# the word with that accent on each vowel.
_TONOS = {"α": "ά", "ε": "έ", "η": "ή", "ι": "ί", "ο": "ό", "υ": "ύ", "ω": "ώ"}


def _greek_variants(tok: str) -> list[str]:
    if not any("Ͱ" <= ch <= "Ͽ" or "ἀ" <= ch <= "῿" for ch in tok):
        return [tok]
    base = "".join(ch for ch in unicodedata.normalize("NFD", tok.lower()) if not unicodedata.combining(ch))
    base = unicodedata.normalize("NFC", base)
    out = [base] + [base[:i] + _TONOS[ch] + base[i + 1:] for i, ch in enumerate(base) if ch in _TONOS]
    return list(dict.fromkeys(out))


def fts_query(q: str) -> str:
    """User text -> safe FTS5 query: every token quoted (so `50 cal`, `let's go`, `$1,500`
    cannot break the parser), implicit AND, the last token a prefix when it is 3+ chars; a Greek
    word becomes an OR of its accent placements."""
    toks = [t.replace('"', "") for t in _TOKEN.findall(q or "")]
    toks = [t for t in toks if t]
    if not toks:
        return ""
    parts = []
    for n, t in enumerate(toks):
        star = "*" if n == len(toks) - 1 and len(t) >= 3 else ""
        vs = _greek_variants(t)
        parts.append(f'"{vs[0]}"{star}' if len(vs) == 1 else "(" + " OR ".join(f'"{v}"{star}' for v in vs) + ")")
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
        raise MsgError(msg("server.bad_value", "This value is not valid: {field}", field=scope))
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
            " speaker, lang, text_en, created_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(rec_id, r["t0"], r["t1"], r["text"], json.dumps(r.get("words") or []), r.get("avg_logprob"),
              r.get("no_speech_prob"), model, r.get("track"), r.get("speaker") or "", r.get("lang") or None,
              r.get("text_en") or None, now)
             for r in sorted(rows, key=lambda r: r["t0"])])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return len(rows)


def attach_translations(rows: list[dict], translated: list[dict], max_gap_s: float = 1.0) -> int:
    """Put whisper's English translation lines beside the original lines: each translated segment goes to the
    original line it overlaps most (or the nearest one within `max_gap_s`); several land in one line in time order.
    Sets row["text_en"]; returns how many rows got one."""
    if not rows:
        return 0
    got: dict[int, list[tuple[float, str]]] = {}
    for ts in translated:
        text = (ts.get("text") or "").strip()
        if not text:
            continue
        t0, t1 = float(ts["t0"]), float(ts["t1"])
        best, best_ov, best_gap = None, 0.0, None
        for k, r in enumerate(rows):
            ov = min(t1, float(r["t1"])) - max(t0, float(r["t0"]))
            if ov > best_ov:
                best, best_ov = k, ov
            elif best_ov <= 0:
                gap = max(float(r["t0"]) - t1, t0 - float(r["t1"]))
                if gap <= max_gap_s and (best_gap is None or gap < best_gap):
                    best, best_gap = k, gap
        if best is not None:
            got.setdefault(best, []).append((t0, text))
    for k, parts in got.items():
        rows[k]["text_en"] = " ".join(t for _t0, t in sorted(parts))
    return len(got)


def segments(db, rec_id: int, with_words: bool = False) -> list[dict]:
    cols = ("id, rec_id, t0, t1, text, avg_logprob, no_speech_prob, model, track, speaker, lang, text_en"
            + (", words" if with_words else ""))
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
    out = {"n": r.get("n", 0) or 0, "speech_s": round(r.get("speech_s") or 0.0, 1), "model": r.get("model"),
           "utc": r.get("utc")}
    # the recording's main speech language = the one with the most speech time (lines tagged otherwise get a tag)
    langs = db.query("SELECT lang, COUNT(*) n, SUM(t1 - t0) s FROM transcript_segments WHERE rec_id=? AND"
                     " COALESCE(lang, '') != '' GROUP BY lang ORDER BY s DESC", (rec_id,))
    out["langs"] = {x["lang"]: x["n"] for x in langs}
    out["lang"] = langs[0]["lang"] if langs else None
    out["translated"] = db.scalar("SELECT COUNT(*) FROM transcript_segments WHERE rec_id=? AND"
                                  " COALESCE(text_en, '') != ''", (rec_id,)) or 0
    return out


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
                "SELECT s.id seg_id, s.rec_id, s.t0, s.t1, s.text, s.speaker, s.lang, s.text_en, r.name rec_name,"
                " r.drive, snippet(transcript_fts, 0, '[', ']', '...', 14) snippet, bm25(transcript_fts) rank,"
                " 'text' matched"
                " FROM transcript_fts JOIN transcript_segments s ON s.id = transcript_fts.rowid"
                " JOIN recordings r ON r.id = s.rec_id"
                f" WHERE transcript_fts MATCH ?{where_drive} ORDER BY rank LIMIT ?", (fq, *params_drive, limit))
            if getattr(db, "fts_en", False):
                # the English translations: a line found in both keeps its original-text hit
                seen = {h["seg_id"] for h in rows}
                more = db.query(
                    "SELECT s.id seg_id, s.rec_id, s.t0, s.t1, s.text, s.speaker, s.lang, s.text_en, r.name rec_name,"
                    " r.drive, snippet(transcript_fts_en, 0, '[', ']', '...', 14) snippet,"
                    " bm25(transcript_fts_en) rank, 'text_en' matched"
                    " FROM transcript_fts_en JOIN transcript_segments s ON s.id = transcript_fts_en.rowid"
                    " JOIN recordings r ON r.id = s.rec_id"
                    f" WHERE transcript_fts_en MATCH ?{where_drive} ORDER BY rank LIMIT ?", (fq, *params_drive, limit))
                rows += [h for h in more if h["seg_id"] not in seen]
                rows = sorted(rows, key=lambda h: h["rank"])[:limit]
            return {"q": q, "hits": rows, "fts": True}
        except Exception as e:  # noqa: BLE001 - a bad FTS expression must not 500
            logger.warning("[Search] fts failed q=%r err=%s -- LIKE fallback", q, e)
    like = f"%{q}%"
    rows = db.query(
        "SELECT s.id seg_id, s.rec_id, s.t0, s.t1, s.text, s.speaker, s.lang, s.text_en, r.name rec_name, r.drive,"
        " CASE WHEN s.text LIKE ? THEN s.text ELSE s.text_en END snippet, 0 rank,"
        " CASE WHEN s.text LIKE ? THEN 'text' ELSE 'text_en' END matched"
        " FROM transcript_segments s JOIN recordings r ON r.id = s.rec_id"
        f" WHERE (s.text LIKE ? OR COALESCE(s.text_en, '') LIKE ?){where_drive} ORDER BY s.rec_id DESC, s.t0 LIMIT ?",
        (like, like, like, like, *params_drive, limit))
    return {"q": q, "hits": rows, "fts": False}
