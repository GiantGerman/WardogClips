"""Voice highlight events: what the voice track says and how loud it gets.

Two kinds of `events` rows with kind='voice', made by the transcribe stage right after
whisper (while the voice wav still exists):

- subkind 'keyword': a transcript segment containing one of `settings.highlights.keywords`
  ("let's go", "got him", ...). t = the word timestamp of the first keyword when whisper
  gave word timings, else the segment start.
- subkind 'excitement': the voice level jumping well over its recent speaking level
  (shouting). A 0.25 s RMS envelope in dB, a rolling floor over the previous 30 s, runs at
  least `rms_spike_db` over it for >= 0.5 s, merged within 3 s, the strongest ~20 kept.

The floor is taken over windows that carry sound only: the voice track is VOIP, silent
most of the time, so a percentile over every window sits on the silence and every normal
sentence would read as a "spike". The median of the voiced windows of the last 30 s is the
speaking level; with no voiced history the recording-wide voiced median stands in.

Everything here except `write_voice_events` / `recontext` is a pure function over numpy
arrays and dicts so it is unit-testable with synthetic audio.
"""

import json
import logging
import re
import time

import numpy as np

logger = logging.getLogger("wclips.highlights")

STEP_S = 0.25              # envelope window
FLOOR_WINDOW_S = 30.0      # rolling floor looks back this far
FLOOR_PCT = 50.0           # percentile of the voiced windows in the look-back = the speaking level
ACTIVE_DB = -55.0          # a window quieter than this is silence (VOIP gate / nobody talking)
MIN_SPIKE_S = 0.5
BRIDGE_S = 0.25            # a single quieter window inside a shout does not split it
MERGE_S = 3.0
MAX_EXCITEMENT = 20
NEAR_S = 10.0              # a kill / death this close gives a voice event context
CARRY_S = 1.5              # a re-transcribe keeps the user's verdict on an event this close
SCORE_KEYWORD = 0.35
SCORE_EXCITEMENT = 0.25
NEAR_BONUS = 0.2
SILENT_DB = -120.0


# ---- audio ------------------------------------------------------------------------------
def envelope_db(x: np.ndarray, sr: int, step_s: float = STEP_S, block: int = 4096) -> np.ndarray:
    """RMS level in dBFS per `step_s` window (the last partial window is dropped). Squares are
    taken `block` windows at a time: a whole-file float64 square of a multi-hour voice wav
    would double its memory (a 3 h wav is ~0.7 GB of float32 already)."""
    n = int(round(sr * step_s))
    if x is None or n <= 0 or x.size < n:
        return np.zeros(0, np.float32)
    m = x.size // n
    frames = np.asarray(x[: m * n], dtype=np.float32).reshape(m, n)
    block = max(1, int(block))
    rms = np.empty(m, np.float64)
    for b in range(0, m, block):
        rms[b:b + block] = np.sqrt(np.mean(np.square(frames[b:b + block], dtype=np.float64), axis=1))
    with np.errstate(divide="ignore"):
        db = 20.0 * np.log10(np.maximum(rms, 1e-12))
    return np.maximum(db, SILENT_DB).astype(np.float32)


def rolling_floor(env: np.ndarray, step_s: float = STEP_S, window_s: float = FLOOR_WINDOW_S,
                  pct: float = FLOOR_PCT, active_db: float = ACTIVE_DB) -> np.ndarray:
    """Per window: the `pct` percentile of the voiced windows in the previous `window_s`
    (the window itself excluded). No voiced history -> the recording-wide voiced percentile;
    a recording with no voiced window at all -> active_db."""
    env = np.asarray(env, dtype=np.float32)
    out = np.full(env.shape, active_db, np.float32)
    if env.size == 0:
        return out
    voiced = env >= active_db
    glob = float(np.percentile(env[voiced], pct)) if voiced.any() else active_db
    look = max(1, int(round(window_s / step_s)))
    for i in range(env.size):
        lo = max(0, i - look)
        hist = env[lo:i][voiced[lo:i]]
        out[i] = float(np.percentile(hist, pct)) if hist.size >= 4 else glob
    return np.maximum(out, active_db)


def find_spikes(env: np.ndarray, floor: np.ndarray, over_db: float, step_s: float = STEP_S,
                min_s: float = MIN_SPIKE_S, merge_s: float = MERGE_S, bridge_s: float = BRIDGE_S,
                active_db: float = ACTIVE_DB) -> list[dict]:
    """Runs where env - floor >= over_db for at least min_s; runs closer than merge_s merge.
    Each spike: t0, t1, dur_s, rms_db (peak window), over_floor_db (peak excess), t_peak."""
    env = np.asarray(env, dtype=np.float32)
    floor = np.asarray(floor, dtype=np.float32)
    if env.size == 0:
        return []
    hot = ((env - floor) >= over_db) & (env >= active_db)
    # bridge single short dips so one shout is one run
    gap = max(0, int(round(bridge_s / step_s)))
    runs: list[list[int]] = []
    i = 0
    while i < hot.size:
        if not hot[i]:
            i += 1
            continue
        j = i
        while j + 1 < hot.size and hot[j + 1]:
            j += 1
        if runs and gap and i - runs[-1][1] - 1 <= gap:
            runs[-1][1] = j
        else:
            runs.append([i, j])
        i = j + 1
    min_n = max(1, int(round(min_s / step_s)))
    runs = [r for r in runs if r[1] - r[0] + 1 >= min_n]
    merged: list[list[int]] = []
    for s, e in runs:
        if merged and (s - merged[-1][1] - 1) * step_s <= merge_s:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    out = []
    for s, e in merged:
        seg = env[s:e + 1]
        exc = seg - floor[s:e + 1]
        k = int(np.argmax(exc))
        out.append({"t0": round(s * step_s, 2), "t1": round((e + 1) * step_s, 2), "dur_s": round((e - s + 1) * step_s, 2),
                    "rms_db": round(float(seg.max()), 1), "over_floor_db": round(float(exc[k]), 1),
                    "t_peak": round((s + k) * step_s, 2)})
    return out


def excitement_events(x: np.ndarray, sr: int, over_db: float, cap: int = MAX_EXCITEMENT) -> list[dict]:
    """Voice wav samples -> excitement event dicts (the strongest `cap`, in time order)."""
    env = envelope_db(x, sr)
    if env.size == 0:
        return []
    spikes = find_spikes(env, rolling_floor(env), over_db)
    for sp in spikes:
        sp["strength"] = sp["over_floor_db"] + 2.0 * min(sp["dur_s"], 3.0)
    spikes = sorted(spikes, key=lambda s: -s["strength"])[:max(0, int(cap))]
    events = []
    for sp in sorted(spikes, key=lambda s: s["t0"]):
        conf = 0.5 + 0.25 * min(1.0, max(0.0, sp["over_floor_db"] - over_db) / 10.0) + 0.25 * min(1.0, sp["dur_s"] / 2.0)
        events.append({"kind": "voice", "subkind": "excitement", "t": sp["t0"], "t_end": sp["t1"],
                       "confidence": round(min(1.0, conf), 3),
                       "source": {"rms_db": sp["rms_db"], "over_floor_db": sp["over_floor_db"], "dur_s": sp["dur_s"],
                                  "t_peak": sp["t_peak"]}})
    return events


# ---- transcript ------------------------------------------------------------------------
_APOS = re.compile(r"['’‘`]")
_NONWORD = re.compile(r"[^\w]+", re.UNICODE)


def norm_tokens(text: str) -> list[str]:
    """Lower case, apostrophes dropped (let's == lets), punctuation -> spaces."""
    return [t for t in _NONWORD.sub(" ", _APOS.sub("", (text or "").lower())).split() if t]


def _find(tokens: list[str], kw: list[str]) -> int:
    k = len(kw)
    for i in range(len(tokens) - k + 1):
        if tokens[i:i + k] == kw:
            return i
    return -1


def keyword_events(segments: list[dict], keywords: list[str]) -> list[dict]:
    """One event per segment that holds any keyword; t = the first keyword's word timestamp."""
    kws = [(k, norm_tokens(k)) for k in keywords]
    kws = [(k, toks) for k, toks in kws if toks]
    if not kws:
        return []
    out = []
    for seg in segments:
        toks = norm_tokens(seg.get("text") or "")
        hits = [(k, ktoks) for k, ktoks in kws if _find(toks, ktoks) >= 0]
        if not hits:
            continue
        # word timings: each word's tokens carry that word's t0 / probability
        wtoks, wt, wp = [], [], []
        for w in seg.get("words") or []:
            for tk in norm_tokens(w.get("w") or ""):
                wtoks.append(tk)
                wt.append(float(w.get("t0", seg["t0"])))
                wp.append(float(w.get("p", 1.0) if w.get("p") is not None else 1.0))
        t, probs = None, []
        for _k, ktoks in hits:
            i = _find(wtoks, ktoks)
            if i >= 0:
                if t is None or wt[i] < t:
                    t = wt[i]
                probs += wp[i:i + len(ktoks)]
        timed = t is not None
        if t is None:
            t = float(seg["t0"])
        conf = float(np.mean(probs)) if probs else 0.8
        out.append({"kind": "voice", "subkind": "keyword", "t": round(t, 2), "t_end": round(float(seg.get("t1", t)), 2),
                    "confidence": round(max(0.3, min(1.0, conf)), 3),
                    "source": {"voice": sorted({k for k, _ in hits}), "text": (seg.get("text") or "")[:300],
                               "word_timed": timed}})
    return out


# ---- scoring -----------------------------------------------------------------------------
def score_events(events: list[dict], context: list[tuple[str, float]], factors: dict | None = None,
                 near_s: float = NEAR_S) -> list[dict]:
    """Modest base scores (keyword 0.35, excitement 0.25), +0.2 when a kill / death of the same
    recording is within near_s (source near_kind / near_t), times confidence, times the learned
    factor of the event's signature. source.base_score keeps the unlearned score."""
    from core import learning
    for ev in events:
        src = ev["source"]
        src.pop("near_kind", None)
        src.pop("near_t", None)
        base = SCORE_KEYWORD if ev["subkind"] == "keyword" else SCORE_EXCITEMENT
        near = [(k, t) for k, t in context if abs(t - ev["t"]) <= near_s]
        if near:
            k, t = min(near, key=lambda kt: (abs(kt[1] - ev["t"]), kt[0] != "kill"))
            src["near_kind"], src["near_t"] = k, round(float(t), 2)
            base += NEAR_BONUS
        score = round(base * ev["confidence"], 3)
        learning.apply(ev, score, factors)
    return events


# ---- db ------------------------------------------------------------------------------------
def _context(db, rec_id: int) -> list[tuple[str, float]]:
    return [(r["kind"], float(r["t_s"])) for r in db.query(
        "SELECT kind, t_s FROM events WHERE rec_id=? AND kind IN ('kill', 'death') AND status != 'false'", (rec_id,))]


def write_voice_events(db, rec_id: int, events: list[dict], carry_s: float = CARRY_S) -> dict:
    """Replace the recording's voice events in one transaction. The user's status / note /
    clip in-out / resolve marker carry over from an old event of the same subkind within
    carry_s; an old event the user touched (kept / false / a note) that nothing new matches is
    left in place rather than lost. Returns {"n", "carried", "kept_orphans"}."""
    now = time.time()
    con = db.connect()
    con.execute("BEGIN IMMEDIATE")
    try:
        # read the old rows under the write lock: a Keep / False positive the user clicks between
        # a read outside the transaction and the DELETE would otherwise be lost
        old = [dict(r) for r in con.execute("SELECT * FROM events WHERE rec_id=? AND kind='voice'", (rec_id,))]
        used: set[int] = set()
        rows = []
        for ev in sorted(events, key=lambda e: e["t"]):
            cands = [o for o in old if o["id"] not in used and (o["subkind"] or "") == ev["subkind"]
                     and abs(float(o["t_s"]) - ev["t"]) <= carry_s]
            prev = min(cands, key=lambda o: abs(float(o["t_s"]) - ev["t"])) if cands else None
            if prev:
                used.add(prev["id"])
            shift = ev["t"] - float(prev["t_s"]) if prev else 0.0
            clip_in = prev["clip_in"] + shift if prev and prev.get("clip_in") is not None else None
            clip_out = prev["clip_out"] + shift if prev and prev.get("clip_out") is not None else None
            rows.append((rec_id, ev["t"], ev.get("t_end"), "voice", ev["subkind"], ev["score"], ev["confidence"],
                         json.dumps({**ev["source"], "auto": True}), prev["status"] if prev else "new",
                         prev["resolve_marker"] if prev else None, prev["note"] if prev else "", clip_in, clip_out, now))
        keep = [o["id"] for o in old if o["id"] not in used and (o["status"] != "new" or (o["note"] or "").strip())]
        if keep:
            con.execute("DELETE FROM events WHERE rec_id=? AND kind='voice' AND id NOT IN (%s)" % ",".join("?" * len(keep)),
                        (rec_id, *keep))
        else:
            con.execute("DELETE FROM events WHERE rec_id=? AND kind='voice'", (rec_id,))
        con.executemany(
            "INSERT INTO events (rec_id, t_s, t_end_s, kind, subkind, score, confidence, source, status, resolve_marker,"
            " note, clip_in, clip_out, created_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return {"n": len(rows), "carried": len(used), "kept_orphans": len(keep)}


def build_and_write(db, cfg, rec_id: int, segments: list[dict], excitement: list[dict]) -> dict:
    """The transcribe stage's step: keyword events from the segments + the excitement events
    already computed from the wav, scored with kill / death context and the learned factors,
    written in one transaction."""
    from core import learning
    from core import settings as settings_mod
    events = keyword_events(segments, settings_mod.keywords(cfg)) + [dict(e, source=dict(e["source"])) for e in excitement]
    score_events(events, _context(db, rec_id), learning.factors(db))
    res = write_voice_events(db, rec_id, events)
    res["keyword"] = sum(1 for e in events if e["subkind"] == "keyword")
    res["excitement"] = sum(1 for e in events if e["subkind"] == "excitement")
    logger.info("[Voice] rec=%s keyword=%d excitement=%d carried=%d kept_orphans=%d", rec_id, res["keyword"],
                res["excitement"], res["carried"], res["kept_orphans"])
    return res


def recontext(db, rec_id: int, context: list[tuple[str, float]], factors: dict | None = None) -> int:
    """Re-score the stored voice events of a recording against fresh kill / death times (the
    detect stage calls this: transcribe usually runs first). Returns rows changed."""
    rows = db.query("SELECT id, t_s, subkind, confidence, score, source FROM events WHERE rec_id=? AND kind='voice'", (rec_id,))
    n = 0
    for r in rows:
        try:
            src = json.loads(r["source"] or "{}")
        except json.JSONDecodeError:
            src = {}
        before = json.dumps(src, sort_keys=True)
        ev = {"kind": "voice", "subkind": r["subkind"] or "", "t": float(r["t_s"]),
              "confidence": float(r["confidence"] or 0.0), "source": src}
        score_events([ev], context, factors)
        if ev["score"] != r["score"] or json.dumps(ev["source"], sort_keys=True) != before:
            db.execute("UPDATE events SET score=?, source=? WHERE id=?", (ev["score"], json.dumps(ev["source"]), r["id"]))
            n += 1
    return n
