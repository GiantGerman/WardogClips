"""False-positive learning: the user's Keep / False positive verdicts re-weight the
scores of events that look the same.

An event's *signature* is coarse on purpose so it generalises across recordings: kind +
subkind + which signals backed it (kill-feed row, respawn text, refined, xp / cash on the
ticker, dark-stretch bucket, kill / death context for voice events). Per signature the
verdicts give a Bayesian kept-rate (kept + A) / (kept + false + A + B), normalised by the
prior A / (A + B) so an unseen signature is 1.0, clamped to [0.25, 1.5] and only applied
once MIN_N verdicts exist. The detect stage and the voice events multiply their score by it;
`source.base_score` keeps the unlearned score so rescoring never compounds, and
`source.learned = {sig, factor}` says what was applied.

Ticker noise: when >= 3 events whose ticker texts normalise to the same pattern were all
marked false, the pattern is reported as a suggestion (label it 'other' on the calibration
page); nothing is hidden automatically.
"""

import json
import logging
import re

logger = logging.getLogger("wclips.learning")

PRIOR_A = 2.0              # pseudo-counts: kept
PRIOR_B = 2.0              # pseudo-counts: false
MIN_N = 3                  # verdicts before a signature's factor is applied
FACTOR_LO, FACTOR_HI = 0.25, 1.5
MIN_FALSE_PATTERN = 3
SKIP_KINDS = ("manual",)   # the user's own marks are not detections


def _src(ev: dict) -> dict:
    s = ev.get("source")
    if isinstance(s, dict):
        return s
    try:
        out = json.loads(s or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return out if isinstance(out, dict) else {}


def _dark_bucket(v) -> str | None:
    try:
        d = float(v)
    except (TypeError, ValueError):
        return None
    if d < 10:
        return "dark<10"
    if d <= 20:
        return "dark10-20"
    return "dark>20"


def signature(ev: dict) -> str:
    """`kind/subkind/flags` -- flags sorted, '-' when empty. Works on an in-memory event (source
    dict) and on an events row (source json text)."""
    kind = ev.get("kind") or ""
    sub = ev.get("subkind") or ""
    src = _src(ev)
    flags = []
    if src.get("feed"):
        flags.append("feed")
    if src.get("respawn_text"):
        flags.append("respawn")
    if src.get("refined") is not None:
        flags.append("refined")
    if src.get("xp"):
        flags.append("xp")
    if src.get("cash"):
        flags.append("cash")
    dark = _dark_bucket(src.get("dark_s")) if "dark_s" in src else None
    if dark:
        flags.append(dark)
    if kind == "voice" and src.get("near_kind"):
        flags.append(f"near-{src['near_kind']}")
    return f"{kind}/{sub or '-'}/{'+'.join(sorted(flags)) or '-'}"


def factor(kept: int, false: int, a: float = PRIOR_A, b: float = PRIOR_B, min_n: int = MIN_N,
           lo: float = FACTOR_LO, hi: float = FACTOR_HI) -> float:
    """Posterior kept-rate over the prior kept-rate; 1.0 below min_n verdicts."""
    kept, false = max(0, int(kept)), max(0, int(false))
    if kept + false < min_n:
        return 1.0
    f = ((kept + a) / (kept + false + a + b)) / (a / (a + b))
    return round(min(hi, max(lo, f)), 3)


def _scored_rows(db, where: str = "", params=()) -> list[dict]:
    kinds = ",".join(f"'{k}'" for k in SKIP_KINDS)
    return db.query(f"SELECT id, rec_id, kind, subkind, status, score, source FROM events"
                    f" WHERE kind NOT IN ({kinds}){where}", params)


def priors(db) -> dict[str, dict]:
    """{signature: {"kept": n, "false": n}} over every event with a verdict."""
    out: dict[str, dict] = {}
    for r in _scored_rows(db, " AND status IN ('kept', 'false')"):
        p = out.setdefault(signature(r), {"kept": 0, "false": 0})
        p[r["status"]] += 1
    return out


def factors(db) -> dict[str, float]:
    """{signature: factor} for the signatures whose factor is not 1.0."""
    out = {}
    for sig, p in priors(db).items():
        f = factor(p["kept"], p["false"])
        if f != 1.0:
            out[sig] = f
    return out


def apply(ev: dict, base_score: float, facs: dict | None) -> dict:
    """Score an in-memory event: base x the learned factor of its signature. Records
    source.base_score and source.learned = {sig, factor}."""
    sig = signature(ev)
    f = float((facs or {}).get(sig, 1.0))
    ev["source"]["base_score"] = round(float(base_score), 3)
    ev["source"]["learned"] = {"sig": sig, "factor": f}
    ev["score"] = round(float(base_score) * f, 3)
    return ev


def _base_of(row: dict, src: dict) -> float:
    if src.get("base_score") is not None:
        return float(src["base_score"])
    f = (src.get("learned") or {}).get("factor")
    try:
        f = float(f)
    except (TypeError, ValueError):
        f = 0.0
    return float(row["score"] or 0.0) / f if f > 0 else float(row["score"] or 0.0)


def rescore(db, sig: str | None = None, kind: str | None = None, facs: dict | None = None) -> dict:
    """Re-score every status 'new' event (optionally only those of one signature) from its
    stored base score. Returns {"examined", "rescored" (score changed), "updated" (rows written,
    also when only the base_score / learned bookkeeping was missing)}."""
    facs = factors(db) if facs is None else facs
    where, params = " AND status='new'", []
    if kind:
        where += " AND kind=?"
        params.append(kind)
    examined = changed = written = 0
    for r in _scored_rows(db, where, tuple(params)):
        s = signature(r)
        if sig is not None and s != sig:
            continue
        examined += 1
        src = _src(r)
        base = _base_of(r, src)
        f = float(facs.get(s, 1.0))
        new = round(base * f, 3)
        learned = {"sig": s, "factor": f}
        same_score = new == round(float(r["score"] or 0.0), 3)
        if same_score and src.get("learned") == learned and src.get("base_score") is not None:
            continue
        src["base_score"] = round(base, 3)
        src["learned"] = learned
        db.execute("UPDATE events SET score=?, source=? WHERE id=?", (new, json.dumps(src), r["id"]))
        written += 1
        changed += 0 if same_score else 1
    return {"examined": examined, "rescored": changed, "updated": written}


def on_status_change(db, event_id: int) -> int:
    """Hook for POST /api/events/{id} when the status changed: re-score the 'new' events that
    share this event's signature. Returns how many changed."""
    row = db.one("SELECT id, kind, subkind, status, score, source FROM events WHERE id=?", (event_id,))
    if row is None or row["kind"] in SKIP_KINDS:
        return 0
    sig = signature(row)
    res = rescore(db, sig=sig, kind=row["kind"])
    logger.info("[Learning] verdict event=%s status=%s sig=%s rescored=%d", event_id, row["status"], sig, res["rescored"])
    return res["rescored"]


# ---- ticker noise -----------------------------------------------------------------------
def _pattern(text: str) -> str:
    try:
        from vision.ticker import normalise
    except Exception:  # noqa: BLE001 - the suggestion must not depend on the vision stack
        def normalise(t):
            return re.sub(r"\s+", " ", t.upper().strip())
    return re.sub(r"\s+", " ", re.sub(r"\d[\d,.]*", "#", normalise(text))).strip()


def ticker_suggestions(db, min_false: int = MIN_FALSE_PATTERN) -> list[dict]:
    """Ticker patterns whose events were marked false >= min_false times and never kept. The
    pattern is in the calibration page's `#`-for-numbers form, so POST /api/calibration/labels
    {text: pattern, label: "other"} turns it off."""
    groups: dict[str, dict] = {}
    for r in _scored_rows(db):
        texts = _src(r).get("ticker") or []
        if not isinstance(texts, list):
            continue
        for pat in {_pattern(str(t)) for t in texts if str(t).strip()}:
            g = groups.setdefault(pat, {"pattern": pat, "false": 0, "kept": 0, "new": 0, "kinds": set(), "examples": []})
            g[r["status"]] = g.get(r["status"], 0) + 1
            g["kinds"].add(r["kind"])
            for t in texts:
                if _pattern(str(t)) == pat and t not in g["examples"] and len(g["examples"]) < 3:
                    g["examples"].append(t)
    out = []
    for g in groups.values():
        if g["false"] >= min_false and g["kept"] == 0:
            out.append({"pattern": g["pattern"], "n_false": g["false"], "n_new": g["new"], "kinds": sorted(g["kinds"]),
                        "examples": g["examples"], "suggest_label": "other",
                        "message": f"{g['false']} events from '{g['pattern']}' were all marked false:"
                                   f" label it 'other' on the calibration page to stop detecting it"})
    out.sort(key=lambda s: -s["n_false"])
    return out


def summary(db) -> dict:
    """The learning page: one row per signature seen (verdicts, factor, how many 'new' events
    it re-weights) + the ticker-noise suggestions."""
    sigs: dict[str, dict] = {}
    for r in _scored_rows(db):
        s = signature(r)
        g = sigs.setdefault(s, {"sig": s, "kind": r["kind"], "subkind": r["subkind"] or "", "kept": 0, "false": 0,
                                "n_new_affected": 0})
        if r["status"] in ("kept", "false"):
            g[r["status"]] += 1
        elif r["status"] == "new":
            g["n_new_affected"] += 1
    rows = []
    for g in sigs.values():
        g["n"] = g["kept"] + g["false"]
        g["factor"] = factor(g["kept"], g["false"])
        g["active"] = g["n"] >= MIN_N
        rows.append(g)
    rows.sort(key=lambda g: (not g["active"], -g["n"], -g["n_new_affected"], g["sig"]))
    return {"signatures": rows, "suggestions": ticker_suggestions(db),
            "params": {"prior_kept": PRIOR_A, "prior_false": PRIOR_B, "min_n": MIN_N, "lo": FACTOR_LO, "hi": FACTOR_HI}}
