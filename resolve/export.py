"""Selection model, clip bounds, timecodes and the Resolve-closed fallback (CSV + CMX3600 EDL).

The selection is shared by the three hand-offs (markers, selects timeline, export):

    {rec_ids?: [..], event_ids?: [..], status?: "kept" (default) | "kept,new" | "any" | "new",
     kinds?: "kill,assist,..." (or a list), min_score?: 0, order?: "score" | "time", limit?: 2000}

-> event rows joined with their recording (path, name, fps, duration, start time) plus the clip
bounds ``clip_in`` / ``clip_out`` in seconds. ``status`` never includes ``false``. When
``event_ids`` is given without a ``status`` the status filter is "any" (they were picked explicitly).

Everything here works with Resolve closed; files go to ``paths.OUT_DIR`` (looked up at call
time so the test fixture's temp dir is honoured).
"""

import csv
import logging
import time
from datetime import datetime
from pathlib import Path

from utils import paths

logger = logging.getLogger("wclips.export")

KINDS = ("kill", "assist", "death", "spotted", "revive", "squad", "voice", "manual")
STATUSES = ("new", "kept")
DEFAULT_FPS = 60.0
MAX_EVENTS = 2000
TRANSCRIPT_WINDOW_S = 8.0


class SelectionError(ValueError):
    """Bad selection body (HTTP 400)."""


# ---- selection ------------------------------------------------------------------------------
def _id_list(v, name: str) -> list[int]:
    if v is None or v == "":
        return []
    if isinstance(v, (int, float)):
        v = [v]
    if isinstance(v, str):
        v = [x for x in v.replace(";", ",").split(",") if x.strip()]
    if not isinstance(v, (list, tuple)):
        raise SelectionError(f"{name} must be a list of ids")
    try:
        return [int(x) for x in v]
    except (TypeError, ValueError):
        raise SelectionError(f"{name} must be a list of ids")


def _statuses(v, explicit_ids: bool) -> list[str]:
    if v is None or v == "":
        return list(STATUSES) if explicit_ids else ["kept"]
    if isinstance(v, (list, tuple)):
        parts = [str(x).strip().lower() for x in v]
    else:
        parts = [x.strip().lower() for x in str(v).split(",")]
    if "any" in parts or "all" in parts:
        return list(STATUSES)
    out = [p for p in parts if p in STATUSES]
    if not out:
        raise SelectionError("status must be kept | new | kept,new | any (false is never exported)")
    return out


def _kinds(v) -> list[str]:
    if v is None or v == "":
        return []
    parts = v if isinstance(v, (list, tuple)) else str(v).split(",")
    out = [str(p).strip().lower() for p in parts if str(p).strip()]
    bad = [k for k in out if k not in KINDS]
    if bad:
        raise SelectionError(f"unknown kind(s): {', '.join(bad)}")
    return out


def rec_start(rec: dict) -> float | None:
    """Wall-clock start of a recording (local epoch). A replay's file-name time is when the
    buffer was SAVED (its end); a plain recording's is its start."""
    dur = float(rec.get("duration_s") or 0.0)
    nu = rec.get("name_utc")
    if nu is not None:
        return float(nu) - dur if rec.get("kind") == "replay" else float(nu)
    mt = rec.get("mtime")
    return float(mt) - dur if mt else None


def rec_date(rec: dict) -> str:
    """YYYY-MM-DD of the recording (file-name time, else mtime) for the Resolve bin."""
    t = rec.get("name_utc") or rec.get("mtime")
    if not t:
        return "undated"
    return datetime.fromtimestamp(float(t)).strftime("%Y-%m-%d")


def clip_bounds(ev: dict, duration_s: float | None, cfg) -> tuple[float, float]:
    """(in, out) seconds: the user's clip_in / clip_out when set, else t_s - pre_roll /
    t_s + post_roll, clamped to [0, duration]."""
    t = float(ev["t_s"])
    a = ev.get("clip_in")
    b = ev.get("clip_out")
    a = float(a) if a is not None else t - float(cfg.highlights.pre_roll_s)
    b = float(b) if b is not None else t + float(cfg.highlights.post_roll_s)
    dur = float(duration_s) if duration_s else None
    one = 1.0 / DEFAULT_FPS
    a = max(0.0, a)
    if dur:
        a = min(a, dur)
        b = min(b, dur)
    if b <= a:                      # user bounds inverted / event at the very end: one frame
        b = a + one
        if dur and b > dur:
            b = dur
            a = max(0.0, b - one)
    return round(a, 4), round(b, 4)


def select(db, cfg, body: dict | None) -> list[dict]:
    body = body or {}
    if not isinstance(body, dict):
        raise SelectionError("body must be an object")
    rec_ids = _id_list(body.get("rec_ids"), "rec_ids")
    event_ids = _id_list(body.get("event_ids"), "event_ids")
    statuses = _statuses(body.get("status"), bool(event_ids))
    kinds = _kinds(body.get("kinds"))
    try:
        min_score = float(body.get("min_score") or 0.0)
        limit = max(1, min(int(body.get("limit") or MAX_EVENTS), MAX_EVENTS))
    except (TypeError, ValueError):
        raise SelectionError("min_score / limit must be numbers")
    order = str(body.get("order") or "score").lower()
    if order not in ("score", "time"):
        raise SelectionError("order must be score | time")

    where = ["e.status IN (%s)" % ",".join("?" * len(statuses))]
    params: list = list(statuses)
    if rec_ids:
        where.append("e.rec_id IN (%s)" % ",".join("?" * len(rec_ids)))
        params += rec_ids
    if event_ids:
        where.append("e.id IN (%s)" % ",".join("?" * len(event_ids)))
        params += event_ids
    if kinds:
        where.append("e.kind IN (%s)" % ",".join("?" * len(kinds)))
        params += kinds
    if min_score:
        where.append("e.score >= ?")
        params.append(min_score)
    rows = db.query(
        "SELECT e.*, r.path rec_path, r.name rec_name, r.drive rec_drive, r.kind rec_kind, r.name_utc rec_name_utc,"
        " r.mtime rec_mtime, r.duration_s rec_duration_s, r.fps rec_fps, r.missing rec_missing FROM events e"
        " JOIN recordings r ON r.id=e.rec_id WHERE " + " AND ".join(where), params)
    out = []
    for ev in rows:
        rec = {"kind": ev["rec_kind"], "name_utc": ev["rec_name_utc"], "mtime": ev["rec_mtime"],
               "duration_s": ev["rec_duration_s"]}
        ev["fps"] = float(ev["rec_fps"] or DEFAULT_FPS)
        ev["rec_start"] = rec_start(rec)
        ev["rec_date"] = rec_date(rec)
        ev["clip_in_s"], ev["clip_out_s"] = clip_bounds(ev, ev["rec_duration_s"], cfg)
        out.append(ev)
    if order == "time":
        out.sort(key=lambda e: ((e["rec_start"] or 0.0), e["rec_id"], e["t_s"]))
    else:
        out.sort(key=lambda e: (-float(e["score"] or 0.0), (e["rec_start"] or 0.0), e["t_s"]))
    return out[:limit]


def merge_clips(events: list[dict], order: str = "score") -> list[dict]:
    """Overlapping (or touching) clip ranges of the same recording become one clip. A clip
    keeps its events (time order), the best score and the earliest start."""
    by_rec: dict[int, list[dict]] = {}
    for ev in events:
        by_rec.setdefault(ev["rec_id"], []).append(ev)
    clips = []
    for rec_id, evs in by_rec.items():
        evs = sorted(evs, key=lambda e: (e["clip_in_s"], e["t_s"]))
        cur = None
        for ev in evs:
            if cur is not None and ev["clip_in_s"] <= cur["clip_out_s"] + 1e-6:
                cur["clip_out_s"] = max(cur["clip_out_s"], ev["clip_out_s"])
                cur["events"].append(ev)
                cur["score"] = max(cur["score"], float(ev["score"] or 0.0))
                continue
            cur = {"rec_id": rec_id, "rec_path": ev["rec_path"], "rec_name": ev["rec_name"], "rec_date": ev["rec_date"],
                   "rec_start": ev["rec_start"], "fps": ev["fps"], "duration_s": ev["rec_duration_s"],
                   "clip_in_s": ev["clip_in_s"], "clip_out_s": ev["clip_out_s"], "events": [ev],
                   "score": float(ev["score"] or 0.0)}
            clips.append(cur)
    for c in clips:
        top = max(c["events"], key=lambda e: (float(e["score"] or 0.0), -e["t_s"]))
        c["kind"] = top["kind"]
        c["event_ids"] = [e["id"] for e in c["events"]]
    if order == "time":
        clips.sort(key=lambda c: ((c["rec_start"] or 0.0), c["rec_id"], c["clip_in_s"]))
    else:
        clips.sort(key=lambda c: (-c["score"], (c["rec_start"] or 0.0), c["clip_in_s"]))
    return clips


# ---- text helpers ---------------------------------------------------------------------------
def marker_name(ev: dict) -> str:
    kind = (ev.get("kind") or "event").lower()
    killer, victim = (ev.get("killer") or "").strip(), (ev.get("victim") or "").strip()
    if kind == "death":
        name = "DEATH by " + killer if killer else "DEATH"
        if ev.get("subkind") == "downed":
            name += " (downed)"
        return name
    if kind in ("kill", "assist", "spotted", "revive", "squad"):
        return f"{kind.upper()} {victim}".strip()
    if kind == "manual":
        note = (ev.get("note") or "").strip().splitlines()
        return ("MANUAL " + note[0][:40]).strip() if note else "MANUAL"
    return kind.upper()


def names_text(ev: dict) -> str:
    killer, victim = (ev.get("killer") or "").strip(), (ev.get("victim") or "").strip()
    if killer and victim:
        return f"{killer} -> {victim}"
    return killer or victim


def transcript_near(db, rec_id: int, t: float, window_s: float = TRANSCRIPT_WINDOW_S, max_chars: int = 400) -> str:
    rows = db.query("SELECT text FROM transcript_segments WHERE rec_id=? AND t1 >= ? AND t0 <= ? ORDER BY t0",
                    (rec_id, t - window_s, t + window_s))
    text = " ".join((r["text"] or "").strip() for r in rows if (r["text"] or "").strip())
    return text if len(text) <= max_chars else text[: max_chars - 3].rstrip() + "..."


def marker_note(ev: dict, transcript: str = "") -> str:
    parts = []
    n = names_text(ev)
    if n:
        parts.append(n)
    if ev.get("weapon"):
        parts.append(f"weapon {ev['weapon']}")
    if ev.get("amount"):
        parts.append(f"+${int(ev['amount']):,}")
    parts.append(f"at {fmt_clock(float(ev['t_s']))} score {float(ev.get('score') or 0):.1f}")
    if (ev.get("note") or "").strip():
        parts.append("note: " + ev["note"].strip())
    if transcript:
        parts.append('voice: "' + transcript + '"')
    parts.append(f"WardogClips event {ev['id']}")
    return " | ".join(parts)


# ---- timecode -------------------------------------------------------------------------------
def tc_base(fps: float | None) -> int:
    """Non-drop timecode base for a frame rate (59.94 / 60 -> 60)."""
    return max(1, int(round(float(fps or DEFAULT_FPS))))


def frames_at(t_s: float, fps: float | None) -> int:
    return int(round(float(t_s) * float(fps or DEFAULT_FPS)))


def tc(frames: int, base: int) -> str:
    frames = max(0, int(frames))
    ff = frames % base
    s = frames // base
    return f"{s // 3600:02d}:{(s // 60) % 60:02d}:{s % 60:02d}:{ff:02d}"


def tc_at(t_s: float, fps: float | None) -> str:
    return tc(frames_at(t_s, fps), tc_base(fps))


def fmt_clock(t_s: float) -> str:
    t_s = max(0.0, float(t_s))
    m, s = divmod(t_s, 60.0)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:04.1f}" if h else f"{m:02d}:{s:04.1f}"


# ---- files ----------------------------------------------------------------------------------
def _out_path(ext: str, stamp: str) -> Path:
    out = Path(paths.OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"wardogclips_{stamp}.{ext}"
    n = 2
    while p.exists():
        p = out / f"wardogclips_{stamp}_{n}.{ext}"
        n += 1
    return p


CSV_COLUMNS = ("event_id", "file", "path", "recording_start", "event_wall_time", "event_tc", "event_s", "kind",
               "subkind", "names", "killer", "victim", "weapon", "amount", "score", "confidence", "status", "note",
               "clip_in_tc", "clip_out_tc", "clip_in_s", "clip_out_s", "transcript", "resolve_marker")


def _wall(t: float | None) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else ""


def write_csv(db, events: list[dict], path: Path) -> int:
    # utf-8-sig so Excel shows the Korean / accented names in the kill feed correctly
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for ev in events:
            fps = ev["fps"]
            start = ev.get("rec_start")
            w.writerow([
                ev["id"], ev["rec_name"], ev["rec_path"], _wall(start),
                _wall(start + float(ev["t_s"])) if start else "", tc_at(ev["t_s"], fps), round(float(ev["t_s"]), 3),
                ev["kind"], ev.get("subkind") or "", names_text(ev), ev.get("killer") or "", ev.get("victim") or "",
                ev.get("weapon") or "", ev.get("amount") if ev.get("amount") is not None else "",
                round(float(ev.get("score") or 0), 2), round(float(ev.get("confidence") or 0), 2), ev["status"],
                ev.get("note") or "", tc_at(ev["clip_in_s"], fps), tc_at(ev["clip_out_s"], fps),
                ev["clip_in_s"], ev["clip_out_s"], transcript_near(db, ev["rec_id"], float(ev["t_s"])),
                ev.get("resolve_marker") or ""])
    return len(events)


def edl_text(clips: list[dict], title: str, record_start_tc: str = "01:00:00:00") -> str:
    """CMX3600, non-drop. Source timecodes count frames of the recording from 00:00:00:00 (OBS
    mp4s carry no start timecode, Resolve reads them as 00:00:00:00); the record side starts
    at 01:00:00:00 and runs continuously. Reel AX + FROM CLIP NAME / SOURCE FILE comments so
    Resolve and Premiere relink by file name."""
    rec_base = tc_base(clips[0]["fps"]) if clips else 60
    hh, mm, ss, ff = (int(x) for x in record_start_tc.split(":"))
    rec_pos = ((hh * 60 + mm) * 60 + ss) * rec_base + ff
    lines = [f"TITLE: {title}", "FCM: NON-DROP FRAME", ""]
    for i, c in enumerate(clips, 1):
        fps = c["fps"]
        base = tc_base(fps)
        src_in = frames_at(c["clip_in_s"], fps)
        src_out = max(src_in + 1, frames_at(c["clip_out_s"], fps))
        # both sides count NDF frames: a 59.94 clip on a base-60 record side is frame-for-frame
        # (dividing by the real 59.94 made record 1 frame longer than source = a speed change)
        dur_rec = max(1, int(round((src_out - src_in) * rec_base / base)))
        lines.append(f"{i:03d}  {'AX':<8} {'AA/V':<5} C        {tc(src_in, base)} {tc(src_out, base)} "
                     f"{tc(rec_pos, rec_base)} {tc(rec_pos + dur_rec, rec_base)}")
        lines.append(f"* FROM CLIP NAME: {c['rec_name']}")
        lines.append(f"* SOURCE FILE: {c['rec_path']}")
        names = "; ".join(marker_name(e) for e in c["events"])
        lines.append(f"* COMMENT: {names} | events {','.join(str(x) for x in c['event_ids'])} | score {c['score']:.1f}")
        lines.append("")
        rec_pos += dur_rec
    return "\r\n".join(lines) + "\r\n"


def write_edl(clips: list[dict], path: Path, title: str) -> int:
    path.write_text(edl_text(clips, title), encoding="utf-8", newline="")
    return len(clips)


def export(db, cfg, body: dict | None, fmt: str = "both") -> dict:
    """Write the CSV and / or EDL of the selection; returns {files: [{name, path, kind, rows}], ...}."""
    fmt = (fmt or "both").lower()
    if fmt not in ("csv", "edl", "both"):
        raise SelectionError("format must be csv | edl | both")
    body = body or {}
    order = str(body.get("order") or "score").lower()
    events = select(db, cfg, body)
    if not events:
        raise SelectionError("no events match the selection")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    files = []
    t0 = time.monotonic()
    if fmt in ("csv", "both"):
        p = _out_path("csv", stamp)
        n = write_csv(db, events, p)
        files.append({"name": p.name, "path": str(p), "kind": "csv", "rows": n})
    clips = merge_clips(events, order)
    if fmt in ("edl", "both"):
        p = _out_path("edl", stamp)
        title = f"WardogClips Selects {datetime.now().strftime('%Y-%m-%d')}"
        n = write_edl(clips, p, title)
        files.append({"name": p.name, "path": str(p), "kind": "edl", "rows": n})
    logger.info("[Export] wrote format=%s events=%d clips=%d files=%s ms=%.0f", fmt, len(events), len(clips),
                ",".join(f["name"] for f in files), (time.monotonic() - t0) * 1000)
    return {"ok": True, "events": len(events), "clips": len(clips), "files": files, "out_dir": str(paths.OUT_DIR)}


def safe_out_file(name: str) -> Path | None:
    """A file directly inside OUT_DIR by bare name; None for anything else (no traversal)."""
    if not name or name != Path(name).name or name in (".", "..") or "/" in name or "\\" in name or ":" in name:
        return None
    root = Path(paths.OUT_DIR).resolve()
    p = (root / name).resolve()
    if p.parent != root or not p.is_file():
        return None
    return p
