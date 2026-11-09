"""Queue moves: put queued jobs at the top / bottom, one step up / down, or before / after another job --
picked by job id, by recording or by stage type -- with ONE SQL update on the existing priority scheme
(the queue claims by priority DESC, id ASC; a plain enqueue's priority is the recording's epoch minutes).

"Move to top" also gives the recordings it moved a *boost* (table ``queue_boost``): ``JobQueue.enqueue``
raises a new job of a boosted recording to at least that priority, so the follow-ups a probe or a
transcribe queues later (transcribe, detect, proxy) stay on top instead of falling back to their date.
A boost ends when the recording has nothing queued or running any more, or when it is moved to the bottom.
"""

from __future__ import annotations

import json
import logging
import time

from core.messages import MsgError, msg

logger = logging.getLogger("wclips.jobs")

BOOST_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue_boost (           -- recordings moved to the top: later jobs inherit the priority
    rec_id        INTEGER PRIMARY KEY,
    priority      INTEGER NOT NULL,
    utc           REAL NOT NULL
);
"""

TARGETS = ("top", "bottom", "up", "down", "before", "after")
# the stages "queue the missing ones" may add, in pipeline order
MISSING_STAGES = ("probe", "transcribe", "detect")


def ensure(db) -> None:
    db.connect().executescript(BOOST_SCHEMA)
    prune(db)                                       # boosts left behind by jobs removed some other way


def boosted(db, rec_id: int | None, priority: int) -> int:
    """The priority a new job of `rec_id` gets: its own, or the recording's boost when that is higher."""
    if rec_id is None:
        return priority
    # only while the recording still has work queued / running (a follow-up is queued while its parent
    # job runs): a boost row that outlived its work must not lift a job queued days later
    b = db.scalar("SELECT b.priority FROM queue_boost b WHERE b.rec_id=? AND EXISTS (SELECT 1 FROM jobs j"
                  " WHERE j.rec_id=b.rec_id AND j.state IN ('queued','running'))", (rec_id,))
    return max(int(priority), int(b)) if b is not None else priority


def prune(db, rec_id: int | None = None) -> int:
    """Drop boosts of recordings with nothing queued or running (one recording, or all)."""
    sql = ("DELETE FROM queue_boost WHERE {where} NOT EXISTS (SELECT 1 FROM jobs j WHERE j.rec_id=queue_boost.rec_id"
           " AND j.state IN ('queued','running'))")
    if rec_id is None:
        return db.execute(sql.format(where=""))
    return db.execute(sql.format(where="rec_id=? AND"), (rec_id,))


def live_boost_top(con, exclude=()) -> int:
    """The highest boost of a recording that still has work queued / running (0 = none), leaving out
    `exclude`; a move to the top or a drag goes above it, so the latest choice wins."""
    ex = set(exclude)
    rows = con.execute("SELECT b.rec_id, b.priority FROM queue_boost b WHERE EXISTS (SELECT 1 FROM jobs j"
                       " WHERE j.rec_id=b.rec_id AND j.state IN ('queued','running'))").fetchall()
    return max((int(r[1]) for r in rows if r[0] not in ex), default=0)


def boosts(db) -> dict[int, int]:
    return {r["rec_id"]: r["priority"] for r in db.query("SELECT rec_id, priority FROM queue_boost")}


def parse_target(to) -> tuple[str, int | None]:
    """'top' | 'bottom' | 'up' | 'down' | {'before': id} | {'after': id} | 'before:<id>' | 'after:<id>'."""
    if isinstance(to, dict):
        for k in ("before", "after"):
            if k in to:
                return k, int(to[k])
        raise MsgError(msg("server.bad_value", "This value is not valid: {field}", field="to"))
    s = str(to or "").strip().lower()
    if ":" in s:
        k, _, v = s.partition(":")
        if k in ("before", "after"):
            return k, int(v)
    if s in ("top", "bottom", "up", "down"):
        return s, None
    raise MsgError(msg("server.bad_value", "This value is not valid: {field}", field="to"))


def missing_stages(cfg, rec: dict) -> list[str]:
    """What still has to run for a recording: probe first (its follow-ups follow Settings > Jobs);
    once probed, transcribe (when speech-to-text is on) and detect if they never finished."""
    if rec.get("missing"):
        return []
    try:
        st = json.loads(rec.get("stages") or "{}")
    except (TypeError, json.JSONDecodeError):
        st = {}
    done = lambda s: (st.get(s) or {}).get("state") == "done"    # noqa: E731
    if rec.get("status") != "probed" or not done("probe"):
        return ["probe"]
    out = []
    if cfg.stt.enabled and not done("transcribe"):
        out.append("transcribe")
    if not done("detect"):
        out.append("detect")
    return out


def _queue_missing(q, rec_ids: list[int]) -> list[int]:
    """Queue the missing stages of recordings that have nothing queued or running."""
    from core.library import _priority, parse_name
    marks = ",".join("?" for _ in rec_ids)
    busy = {r["rec_id"] for r in q.db.query(
        f"SELECT DISTINCT rec_id FROM jobs WHERE state IN ('queued','running') AND rec_id IN ({marks})", tuple(rec_ids))}
    out = []
    for rid in rec_ids:
        if rid in busy:
            continue
        rec = q.db.one("SELECT * FROM recordings WHERE id=?", (rid,))
        if rec is None:
            continue
        if rec.get("status") == "error":
            q.db.execute("UPDATE recordings SET status='new', error=NULL WHERE id=?", (rid,))
            rec["status"] = "new"
        pri = _priority(parse_name(rec["name"])[1], rec.get("mtime") or 0)
        for stage in missing_stages(q.cfg, rec):
            jid = q.enqueue(rid, stage, priority=pri)
            if jid is not None:
                out.append(jid)
    return out


def _strs(v) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, (list, tuple)):
        raise MsgError(msg("server.bad_value", "This value is not valid: {field}", field="stages"))
    return [str(x) for x in v]


def _ints(v) -> list[int]:
    if v is None:
        return []
    if not isinstance(v, (list, tuple)):
        raise MsgError(msg("server.bad_value", "This value is not valid: {field}", field="ids"))
    return [int(x) for x in v]


def move(q, ids=None, rec_ids=None, stages=None, to="top", queue_missing: bool = False) -> dict:
    """Move queued jobs as one block (see the module doc). Selection, in block order: `ids` as listed,
    then each of `rec_ids` (as listed) with its queued jobs in claim order, then -- with only `stages`
    given -- every queued job of those stages in claim order; `stages` also narrows the rec_ids part.
    Returns {moved, ids, boosted, queued, idle, ms}."""
    mode, anchor = parse_target(to)
    ids, rec_ids = _ints(ids), _ints(rec_ids)
    stages = _strs(stages)
    if not (ids or rec_ids or stages):
        raise MsgError(msg("server.queue.nothing_to_move", "There is nothing to move."))
    t0 = time.perf_counter()
    new_ids = _queue_missing(q, rec_ids) if (rec_ids and queue_missing) else []
    con = q.db.connect()
    out = {"moved": 0, "ids": [], "boosted": [], "queued": new_ids, "idle": []}
    with q._claim_lock:
        con.execute("BEGIN IMMEDIATE")
        try:
            rows = con.execute("SELECT id, rec_id, stage, lane, priority FROM jobs WHERE state='queued'"
                               " ORDER BY priority DESC, id ASC").fetchall()
            order = [r["id"] for r in rows]
            info = {r["id"]: r for r in rows}
            idx = {jid: i for i, jid in enumerate(order)}
            # -- the block
            block, seen = [], set()
            for jid in ids:
                if jid in info and jid not in seen:
                    block.append(jid)
                    seen.add(jid)
            rec_jobs: dict[int, list[int]] = {}
            if rec_ids:
                want = set(rec_ids)
                for jid in order:
                    r = info[jid]
                    if r["rec_id"] in want and (not stages or r["stage"] in stages):
                        rec_jobs.setdefault(r["rec_id"], []).append(jid)
                for rid in dict.fromkeys(rec_ids):
                    for jid in rec_jobs.get(rid, []):
                        if jid not in seen:
                            block.append(jid)
                            seen.add(jid)
            elif stages and not ids:
                for jid in order:
                    if info[jid]["stage"] in stages:
                        block.append(jid)
                        seen.add(jid)
            running = set()
            if rec_ids:
                marks = ",".join("?" for _ in rec_ids)
                running = {x[0] for x in con.execute(
                    f"SELECT DISTINCT rec_id FROM jobs WHERE state='running' AND rec_id IN ({marks})", tuple(rec_ids))}
            out["idle"] = [rid for rid in dict.fromkeys(rec_ids) if rid not in rec_jobs and rid not in running]
            others = [jid for jid in order if jid not in seen]
            changes: dict[int, int] = {}
            boost_set: dict[int, int] = {}
            boost_clear: set[int] = set()
            if mode == "top":
                # entries top-down: the block, plus a slot under each moved recording's jobs for its later
                # follow-ups (a recording that only has a RUNNING job gets its slot too)
                by_id = list(dict.fromkeys(j for j in ids if j in seen))
                entries: list[tuple[str, int]] = [("job", jid) for jid in by_id]
                placed = set(by_id)
                for rid in dict.fromkeys(rec_ids):
                    for jid in rec_jobs.get(rid, []):
                        if jid not in placed:
                            entries.append(("job", jid))
                            placed.add(jid)
                    if rid in rec_jobs or rid in running:
                        entries.append(("rec", rid))
                stage_only = bool(stages) and not ids and not rec_ids
                if stage_only:
                    entries = [("job", jid) for jid in block]
                if entries:
                    top = max((info[j]["priority"] for j in others), default=0)
                    # a recording moved to the top EARLIER that is still being worked on: its next steps
                    # land at its boost, so this (later) move goes above that too -- the latest move wins
                    moved_recs = {rid for kind, rid in entries if kind == "rec"} |                                  {info[j]["rec_id"] for kind, j in entries if kind == "job"}
                    top = max(int(top), live_boost_top(con, exclude=moved_recs))
                    base = max(int(top), int(time.time() // 60) + q_headroom()) + 1
                    n = len(entries)
                    for i, (kind, v) in enumerate(entries):
                        p = base + n - 1 - i
                        if kind == "job":
                            changes[v] = p
                        else:
                            boost_set[v] = p
                    # recordings moved by job id: their follow-ups sit right under their lowest moved job
                    if not stage_only:
                        slots = set(boost_set)          # recordings moved by rec_ids keep their own slot
                        for jid in by_id:
                            rid = info[jid]["rec_id"]
                            if rid is None or rid in slots:
                                continue
                            boost_set[rid] = min(boost_set.get(rid, changes[jid]), changes[jid])
                    out["moved"] = len(block)
            elif not block:
                pass
            elif mode == "bottom":
                _bottom(block, others, info, changes)
                boost_clear = {info[j]["rec_id"] for j in block if info[j]["rec_id"] is not None} | set(rec_ids)
                out["moved"] = len(block)
            else:
                k = _insert_at(mode, anchor, block, others, order, info, idx)
                if k is not None:
                    if k >= len(others):
                        _bottom(block, others, info, changes)
                    else:
                        new = others[:k] + block + others[k:]
                        e = k + len(block) - 1
                        nxt = new[e + 1]
                        p_next, id_next = info[nxt]["priority"], nxt
                        for i in range(e, -1, -1):
                            jid = new[i]
                            p = max(int(info[jid]["priority"]), p_next + (0 if jid < id_next else 1))
                            if p != info[jid]["priority"]:
                                changes[jid] = p
                            p_next, id_next = p, jid
                    out["moved"] = len(block)
            if changes:
                # '+jobs.state': keeps the planner off idx_jobs_state, which would re-read the whole json per
                # queued row (n x m: 40 ms for 200 of 500); this way it is one rowid lookup per pair (< 1 ms)
                con.execute("UPDATE jobs SET priority=json_extract(o.value, '$[1]') FROM json_each(?) o"
                            " WHERE jobs.id=json_extract(o.value, '$[0]') AND +jobs.state='queued'",
                            (json.dumps([[j, p] for j, p in changes.items()]),))
            if boost_set:
                con.execute("INSERT INTO queue_boost (rec_id, priority, utc) SELECT json_extract(o.value, '$[0]'),"
                            " json_extract(o.value, '$[1]'), ? FROM json_each(?) o WHERE 1"
                            " ON CONFLICT(rec_id) DO UPDATE SET priority=excluded.priority, utc=excluded.utc",
                            (time.time(), json.dumps([[r, p] for r, p in boost_set.items()])))
            if boost_clear:
                con.execute("DELETE FROM queue_boost WHERE rec_id IN (SELECT value FROM json_each(?))",
                            (json.dumps(sorted(boost_clear)),))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    out["ids"] = block
    out["boosted"] = sorted(boost_set)
    out["ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
    if out["moved"] or new_ids:
        q.bus.publish("jobs", action="reordered", n=out["moved"])
        q._wake.set()
    logger.info("[Jobs] move to=%s jobs=%d recordings=%d boosted=%d queued=%d changed=%d %.1fms", mode, out["moved"],
                len(rec_ids), len(boost_set), len(new_ids), len(changes), out["ms"])
    return out


def q_headroom() -> int:
    from core.jobs import REORDER_HEADROOM
    return REORDER_HEADROOM


def _bottom(block, others, info, changes) -> None:
    """Below every other queued job, in block order (a new enqueue may still land under it: newer footage)."""
    if others:
        floor = min(int(info[j]["priority"]) for j in others)
    else:
        floor = min(int(info[j]["priority"]) for j in block) + len(block)
    for i, jid in enumerate(block):
        p = floor - 1 - i
        if p != info[jid]["priority"]:
            changes[jid] = p


def _insert_at(mode, anchor, block, others, order, info, idx) -> int | None:
    """Where in `others` (the queue without the block) the block goes; None = nowhere to go."""
    pos_in_others = {jid: i for i, jid in enumerate(others)}
    if mode in ("before", "after"):
        if anchor not in pos_in_others:
            raise MsgError(msg("server.queue.anchor_gone", "The job to move next to is not waiting anymore (or is one of the moved jobs)."))
        return pos_in_others[anchor] + (1 if mode == "after" else 0)
    blockset = set(block)
    if mode == "up":
        first = min(block, key=lambda j: idx[j])
        lane = info[first]["lane"]
        above = [order[i] for i in range(idx[first] - 1, -1, -1) if order[i] not in blockset]
        if not above:
            return None
        # the job that would run before it in the same lane (else simply the row above)
        tgt = next((j for j in above if info[j]["lane"] == lane), above[0])
        return pos_in_others[tgt]
    last = max(block, key=lambda j: idx[j])
    lane = info[last]["lane"]
    below = [order[i] for i in range(idx[last] + 1, len(order)) if order[i] not in blockset]
    if not below:
        return None
    tgt = next((j for j in below if info[j]["lane"] == lane), below[0])
    return pos_in_others[tgt] + 1
