"""The `detect` stage: kills, assists, deaths, spotted-target credits and revives from the
HUD, with evidence crops and frame-accurate times.

Three passes over one recording:
1. keyframes (every 2 s, NVDEC): the reward ticker is OCR'd whenever the area under the
   money readout holds enough white pixels (a line is showing); the money figures come
   free from the same OCR; the shrunken full frame gives the respawn-screen darkness.
2. feed: the kill feed is OCR'd only at the keyframes round each candidate (names, the
   `[60 m]` distance tag that marks rows involving the player, multi-kills).
3. refine: a 3.5 s window at the full frame rate round each kill, template-matching the
   ticker line seen at the keyframe, gives the frame it first appeared; deaths refine on
   the first dark frame. Evidence crops are grabbed at the refined time.

Every signal that agreed is recorded in `events.source`; confidence is their sum.

Where the HUD is: a zone template (core/zones.py -- pinned to the recording, or the streamer's
version for the recording's date) gives the ticker / feed / money / center boxes; without one the
built-in Season 2 boxes of vision/hud.py apply. `stages.detect` records
`template_id` + `zones_source`, `recordings.hud_profile` = `tpl:<id>` or the hud profile.
"""

import difflib
import json
import logging
import os
import re
import time

import cv2
import numpy as np

from core import checkpoints
from core.library import rec_to_dict
from utils import imgio, paths
from vision import feed as feed_mod
from vision import frames, gpu_guard, hud
from vision import ticker as ticker_mod
from vision.ocr import ocr, ocr_fast, white_count, white_mask

logger = logging.getLogger("wclips.detect")

WHITE_MIN = 250            # white pixels under the money readout before the ticker crop is OCR'd
DARK_MEAN = 16.0           # mean brightness of the black respawn screen (gameplay is 40-120)
DARK_MAX_S = 20.0          # a confirmed death whose dark stretch runs longer than this was a long down, not a respawn
DEDUPE_S = 6.0             # the same ticker line seen again within this window is one sighting
# Reuse of the last ticker reading: a gated keyframe whose ticker crop matches the crop that was last really OCR'd
# -- whole-crop mean |diff| <= REUSE_MEAN AND at most REUSE_TXT text-like pixels (bright, unsaturated, dilated, in
# either crop) changed by more than 60 levels -- gets that reading again instead of a new OCR. At most REUSE_MAX
# keyframes in a row (~10 s at keyint 2 s), then it is read again whatever the crop says. In testing this never
# skipped a new money value or ticker line; it rarely fires on OBS footage (the game moves behind the HUD) but saves
# a large share of the reads on stream VODs with a static overlay.
REUSE_MEAN = 2.0
REUSE_TXT = 10
REUSE_MAX = 5
_DILATE = np.ones((5, 5), np.uint8)


def _ticker_unchanged(anchor_gray: np.ndarray, anchor_white: np.ndarray, crop: np.ndarray) -> bool:
    """Is `crop` the same ticker as the last OCR'd one (its grayscale + white mask)?"""
    if anchor_gray.shape != crop.shape[:2]:
        return False
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).astype(np.int16)
    d = np.abs(g - anchor_gray)
    if float(d.mean()) > REUSE_MEAN:
        return False
    text = cv2.dilate(np.maximum(anchor_white, white_mask(crop)), _DILATE) > 0
    return int(((d > 60) & text).sum()) <= REUSE_TXT


def _gpu_off() -> str | None:
    """Why GPU OCR is off until the app restarts (a hung / lost GPU device), or None."""
    try:
        from vision import ocr as _ocr
        return _ocr.gpu_lost()
    except Exception:  # noqa: BLE001
        return None


def _gpu_off_note() -> str:
    why = _gpu_off()
    return f"; GPU off until restart: {why}" if why else ""


class _OcrCount:
    """THIS run's OCR calls / seconds (the engines' own counters are process-wide: parallel detects mix in), and where
    each read ran: `device` = Settings > Detection > ocr_device, decided per call by vision/gpu_guard.py."""

    def __init__(self, device: str = "cpu"):
        self.calls, self.seconds = 0, 0.0
        self.device = device
        self.gpu_calls = 0

    def read(self, engine, img, min_conf) -> tuple[list, bool]:
        ready = getattr(engine, "gpu_ready", None)
        gpu = bool(ready and gpu_guard.guard.use_gpu(self.device, ready()))
        t0 = time.monotonic()
        try:
            rd = getattr(engine, "read_ok", None)
            if rd and gpu:
                return rd(img, min_conf, gpu=True)
            return rd(img, min_conf) if rd else (engine.read(img, min_conf), True)
        finally:
            self.calls += 1
            # where the read really ran (a GPU failure re-reads on the CPU)
            last = getattr(engine, "last_device", None)
            self.gpu_calls += bool(gpu and last and last() == "gpu")
            self.seconds += time.monotonic() - t0
FEED_BEFORE_S, FEED_AFTER_S = 6.0, 8.0
DEATH_FEED_BEFORE_S = 14.0
REFINE_MATCH = 0.75
KINDS = {"kill": "kill", "headshot": "kill", "assist": "assist", "spotted_destroyed": "spotted",
         "revive": "revive", "revive_assist": "revive"}


def _cluster(items, window: float):
    """Sort by t and group runs whose neighbors are within `window`."""
    out, cur = [], []
    for it in sorted(items, key=lambda x: x.t_s):
        if cur and it.t_s - cur[-1].t_s > window:
            out.append(cur)
            cur = []
        cur.append(it)
    if cur:
        out.append(cur)
    return out


def _dark_runs(dark: list[tuple[float, float]], keyint: float) -> list[tuple[float, float, int]]:
    """(start, end, n) runs of keyframes darker than DARK_MEAN."""
    runs, start, prev, n = [], None, None, 0
    for t, mean in dark:
        if mean < DARK_MEAN:
            if start is None:
                start, n = t, 0
            n += 1
            prev = t
        elif start is not None:
            runs.append((start, prev + keyint, n))
            start = None
    if start is not None:
        runs.append((start, prev + keyint, n))
    return runs


def _merge_runs(runs: list[tuple[float, float, int]], gap_s: float = 15.0) -> list[tuple[float, float, int]]:
    """Dark runs closer than gap_s are one death (the death cam fades, the respawn menu fades)."""
    out: list[tuple[float, float, int]] = []
    for s, e, n in runs:
        if out and s - out[-1][1] <= gap_s:
            out[-1] = (out[-1][0], e, out[-1][2] + n)
        else:
            out.append((s, e, n))
    return out


def _nearest(times: list[float], t: float) -> float:
    return min(times, key=lambda x: abs(x - t)) if times else t


def _respawn_text(path: str, t: float, width: int, height: int, min_conf: float, roi: hud.Roi | None = None) -> bool:
    """Is the centered `RESPAWNING IN N...` line on screen at t? (Dark cockpits, bunkers and
    menus are dark too; only the respawn screen says so.) `roi` = a zone template's center box."""
    roi = roi or hud.respawn_roi(width, height)
    try:
        crop = frames.grab_crops(path, t, {"center": roi})["center"]
    except frames.FrameError:
        return False
    for b in ocr.read(crop, min_conf):
        word = re.sub(r"[^A-Z]", "", b.text.upper())
        if "RESPAWN" in word or difflib.SequenceMatcher(None, "RESPAWNING", word[:10]).ratio() >= 0.7:
            return True
    return False


def _same_row(a: feed_mod.Row, b: feed_mod.Row, window_s: float = 20.0) -> bool:
    """The same kill-feed row read twice with OCR noise ([ABC]SquadMate / [ABC]SquadMale)."""
    return (abs(a.t_s - b.t_s) <= window_s and feed_mod.name_matches(a.killer, [b.killer], 0.8)
            and feed_mod.name_matches(a.victim, [b.victim], 0.8))


def _keywords_near(db, rec_id: int, t: float, keywords, span: float = 8.0) -> list[str]:
    """`keywords` = a list, or the per-language map: each line is matched with its own language's keywords plus the
    English ones (a line from before speech languages existed counts as English)."""
    rows = db.query("SELECT text, lang FROM transcript_segments WHERE rec_id=? AND t1 >= ? AND t0 <= ?",
                    (rec_id, t - span, t + span))
    hits = []
    for r in rows:
        low = r["text"].lower()
        from core import settings as settings_mod
        kws = settings_mod.keywords_for(keywords, r.get("lang") or "en")
        hits += [k for k in kws if k in low]
    return sorted(set(hits))


def _zone_geometry(db, rec: dict, width: int, height: int) -> tuple[dict | None, str, dict | None]:
    """(template, source, pixel geometry) from core/zones.py, or (None, 'default', None) = the
    built-in hud boxes. A template that cannot be applied falls back to the built-in boxes with a
    warning rather than failing the run."""
    from core import zones as zones_mod
    try:
        tpl, source = zones_mod.resolve(db, rec)
        if tpl is None:
            return None, source, None
        return tpl, source, zones_mod.geometry(tpl["zones"], width, height)
    except Exception as e:  # noqa: BLE001 - a bad template must not stop detection
        logger.warning("[Detect] zone template not applied rec=%s err=%s -- using the built-in HUD boxes", rec.get("id"), e)
        return None, "default", None


def run(ctx) -> str:
    rec, cfg = ctx.rec, ctx.cfg
    if rec["status"] != "probed" or not rec.get("duration_s") or not rec.get("width"):
        raise RuntimeError("probe the recording first")
    path, duration = rec["path"], float(rec["duration_s"])
    if not os.path.exists(path):
        ctx.db.execute("UPDATE recordings SET missing=1, updated_utc=? WHERE id=?", (time.time(), rec["id"]))
        raise RuntimeError("file is not there any more")
    from vision.ocr import available
    if not available():
        raise RuntimeError("rapidocr is not installed - reinstall WardogClips")
    width, height = int(rec["width"]), int(rec["height"])
    keyint = float(rec.get("keyint_s") or 2.0)
    tpl, zsource, geo = _zone_geometry(ctx.db, rec, width, height)
    if tpl is not None:
        # a streamer's (or a pinned) zone template: their boxes, scaled to this frame
        profile = f"tpl:{tpl['id']}"
        rois, money_y, row_h = geo["rois"], geo["money_y"], geo["feed_row_h"]
        respawn_box, refine_center = geo["center"], geo["center"]
        logger.info("[Detect] rec=%s zones=%s template=%s ticker=%s feed=%s money_y=%d row_h=%d", rec["id"], zsource,
                    tpl["id"], rois["ticker"].box, rois["feed"].box, money_y, row_h)
    else:
        profile = hud.profile_for(width, height, rec.get("name_utc"), cfg.detection.hud_profile)
        rois = hud.rois(width, height, profile)
        money_y = hud.money_y(height, profile)
        row_h = hud.feed_row_h(height, profile)
        respawn_box, refine_center = None, hud.refine_center_roi(width, height)
    rules = [(r["pattern"], r["label"]) for r in ctx.db.query("SELECT pattern, label FROM ticker_labels ORDER BY id")]
    aliases, squad = list(cfg.player.aliases), list(cfg.player.squad)
    from core import settings as settings_mod
    keywords = settings_mod.keyword_map(cfg)            # voice keywords per speech language
    t_start = time.monotonic()
    oc = _OcrCount(cfg.detection.ocr_device)
    every = max(1, int(cfg.detection.ticker_every or 1))     # ticker read on every Nth keyframe
    tick_ocr = ocr_fast if cfg.detection.ocr_fast_ticker else ocr
    reuse_on = bool(cfg.detection.ticker_reuse)

    # ---- pass 1: keyframes -------------------------------------------------------------
    sightings: list[ticker_mod.Line] = []
    templates: dict[int, np.ndarray] = {}
    last_seen: dict[tuple, float] = {}
    money: list[tuple[float, int | None, int | None]] = []
    dark: list[tuple[float, float]] = []
    kf_times: list[float] = []
    n_frames = 0
    anchor = None                      # (gray int16, white mask, boxes) of the last ticker crop really OCR'd
    streak = n_reused = 0
    # resume point (core/checkpoints.py): everything pass 1 depends on; any change -> start over
    fp = {"file": checkpoints.file_stamp(path), "duration": duration, "size": [width, height], "keyint": keyint,
          "profile": profile, "rois": {k: [r.x, r.y, r.w, r.h] for k, r in sorted(rois.items())},
          "money_y": money_y, "row_h": row_h, "rules": rules, "engine": "fast" if tick_ocr is ocr_fast else "full",
          "reuse": reuse_on, "min_conf": cfg.detection.ocr_min_conf, "white_min": WHITE_MIN, "dedupe_s": DEDUPE_S,
          "every": every,
          # the code that made the saved readings: after an update the labels of the part already read could differ
          "code": checkpoints.code_stamp("vision/detect.py", "vision/ticker.py", "vision/ocr.py", "vision/frames.py",
                                         "vision/hud.py", "core/zones.py")}
    last_t, resumed_at, pass_done, in_frame = -1.0, None, False, False
    saved = checkpoints.load("detect", rec["id"], fp)
    if saved:
        sightings = [ticker_mod.Line(t_s=a, text=b, label=c, cash=d, xp=e, conf=f) for a, b, c, d, e, f in saved["sightings"]]
        templates, last_seen = saved["templates"], saved["last_seen"]
        money, dark, kf_times = saved["money"], saved["dark"], saved["kf_times"]
        n_frames, n_reused, last_t = saved["n_frames"], saved["n_reused"], saved["last_t"]
        pass_done = bool(saved.get("pass_done"))
        oc.calls, oc.seconds = saved["ocr_calls"], saved["ocr_seconds"]
        oc.gpu_calls = int(saved.get("ocr_gpu", 0))
        resumed_at = last_t
        logger.info("[Detect] rec=%s resuming the keyframe pass at %.1fs (%d keyframes, %d ticker lines kept)",
                    rec["id"], last_t, n_frames, len(sightings))

    def _checkpoint():
        # only between keyframes: a frame half-way through (an OCR error) would be counted twice on resume
        if n_frames and not in_frame:
            checkpoints.save("detect", rec["id"], fp, {
                "sightings": [(ln.t_s, ln.text, ln.label, ln.cash, ln.xp, ln.conf) for ln in sightings],
                "templates": templates, "last_seen": last_seen, "money": money, "dark": dark, "kf_times": kf_times,
                "n_frames": n_frames, "n_reused": n_reused, "last_t": last_t, "ocr_calls": oc.calls,
                "ocr_seconds": oc.seconds, "ocr_gpu": oc.gpu_calls, "pass_done": pass_done})

    if resumed_at is not None:
        ctx.progress(0.03 + 0.55 * min(1.0, last_t / duration),
                     f"resuming keyframes at {int(last_t) // 60}:{int(last_t) % 60:02d}")
    else:
        ctx.progress(0.02, "keyframe pass")
    next_save = time.monotonic() + checkpoints.CHECKPOINT_EVERY_S
    try:
        kf_iter = () if pass_done else frames.keyframe_pass(path, width, height, rois,
                                                             start_s=int(last_t) if last_t > 0 else 0)
        for t, imgs in kf_iter:
            if t <= last_t + 1e-3:           # a resumed pass seeks to the whole second before the last keyframe done
                continue
            in_frame = True
            n_frames += 1
            kf_times.append(t)
            dark.append((t, float(imgs["small"].mean())))
            tk = imgs["ticker"]
            if (n_frames - 1) % every == 0 and white_count(tk, money_y) >= WHITE_MIN:
                if reuse_on and anchor is not None and streak < REUSE_MAX and _ticker_unchanged(anchor[0], anchor[1], tk):
                    boxes = anchor[2]
                    streak += 1
                    n_reused += 1
                else:
                    boxes, ok = oc.read(tick_ocr, tk, cfg.detection.ocr_min_conf)
                    # a failed inference is never reused: the next gated keyframe reads again
                    anchor = (cv2.cvtColor(tk, cv2.COLOR_BGR2GRAY).astype(np.int16), white_mask(tk), boxes)                     if reuse_on and ok else None
                    streak = 0
                profit, account = ticker_mod.read_money(boxes, money_y)
                if account is not None:
                    money.append((t, profit, account))
                lines = ticker_mod.read_lines(boxes, t, money_y, row_h, rules)
                if anchor is not None and anchor[2] is boxes and any(ln.label == "other" for ln in lines):
                    # an unrecognized line may be a misread (e.g. 'KILLASSST 205XP' for a ticker read right as KILLASSIST
                    # on an identical frame 2 s later): the OCR is not deterministic, so such a reading is never
                    # reused -- the next keyframe gets a fresh read
                    anchor = None
                for line in lines:
                    key = (line.label, line.cash, line.xp, line.text if line.label == "other" else "")
                    if key in last_seen and t - last_seen[key] <= DEDUPE_S:
                        last_seen[key] = t
                        continue
                    last_seen[key] = t
                    sightings.append(line)
                    if line.label in KINDS:
                        y0, y1 = max(0, line.y0 - 4), min(tk.shape[0], line.y1 + 4)
                        x0, x1 = max(0, line.x0 - 4), min(tk.shape[1], line.x1 + 4)
                        templates[len(sightings) - 1] = cv2.cvtColor(tk[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
            last_t, in_frame = t, False
            if n_frames % 10 == 0:
                ctx.progress(0.03 + 0.55 * min(1.0, t / duration), f"keyframes {int(t) // 60}:{int(t) % 60:02d} / {int(duration) // 60}:{int(duration) % 60:02d}")
            if ctx.stopping():
                raise RuntimeError("stopped during the keyframe pass")
            if time.monotonic() >= next_save:
                _checkpoint()
                next_save = time.monotonic() + checkpoints.CHECKPOINT_EVERY_S
    except BaseException:
        _checkpoint()                        # stopped / shut down / ffmpeg died: the next run resumes here
        raise
    if n_frames == 0:
        raise RuntimeError("no frames decoded")
    logger.info("[Detect] rec=%s keyframes=%d ticker_lines=%d money=%d ocr=%d/%.1fs reused=%d engine=%s%s", rec["id"],
                n_frames, len(sightings), len(money), oc.calls, oc.seconds, n_reused,
                "fast" if tick_ocr is ocr_fast else "full",
                f" resumed_at={resumed_at:.1f}s" if resumed_at is not None else "")
    if not pass_done:
        pass_done = True
        _checkpoint()                        # the pass is complete: a crash in passes 2-3 re-runs only those

    # ---- candidates -------------------------------------------------------------------
    cands = []   # dicts: kind, t, label, lines, template
    by_kind: dict[str, list] = {}
    for i, line in enumerate(sightings):
        kind = KINDS.get(line.label)
        if kind:
            by_kind.setdefault(kind, []).append((line, templates.get(i)))
    for kind, items in by_kind.items():
        for group in _cluster([ln for ln, _ in items], DEDUPE_S):
            tmpl = next((tp for ln, tp in items if ln in group and tp is not None), None)
            cands.append({"kind": kind, "t": group[0].t_s, "lines": group, "template": tmpl})
    # every dark stretch is a candidate, however long: being downed keeps the screen dark for
    # minutes, and the feed row / respawn text decide whether it was a death at all
    for start, end, n in _merge_runs(_dark_runs(dark, keyint)):
        if start > keyint:
            cands.append({"kind": "death", "t": start, "lines": [], "template": None, "dark_n": n, "dark_end": end})

    # ---- pass 2: the kill feed round each candidate -----------------------------------
    ctx.progress(0.6, f"kill feed round {len(cands)} candidates")
    want: set[float] = set()
    for c in cands:
        if c["kind"] == "death":
            rng = np.arange(c["t"] - DEATH_FEED_BEFORE_S, c["t"] + keyint, keyint)
        else:
            rng = np.arange(c["t"] - 2 * keyint, c["t"] + FEED_AFTER_S, keyint)
        for t in rng:
            if 0 <= t <= duration:
                want.add(_nearest(kf_times, float(t)))
    rows_all: list[feed_mod.Row] = []
    for k, t in enumerate(sorted(want)):
        try:
            crop = frames.grab_crops(path, t, {"feed": rois["feed"]})["feed"]
        except frames.FrameError as e:
            logger.warning("[Detect] feed grab failed t=%.1f: %s", t, e)
            continue
        boxes, _ok = oc.read(ocr, crop, cfg.detection.ocr_min_conf)
        for row in feed_mod.read_rows(boxes, t, row_h, aliases, squad):
            if not any(_same_row(row, old) for old in rows_all):
                rows_all.append(row)
        if k % 5 == 0:
            ctx.progress(0.6 + 0.15 * (k + 1) / max(1, len(want)), f"kill feed {k + 1}/{len(want)}")
        if ctx.stopping():
            raise RuntimeError("stopped during the feed pass")
    rows_all.sort(key=lambda r: r.t_s)

    # ---- events --------------------------------------------------------------------------
    events = []
    for c in cands:
        if c["kind"] == "kill":
            cash = next((ln.cash for ln in c["lines"] if ln.cash), None)
            xp = next((ln.xp for ln in c["lines"] if ln.xp), None)
            rows = [r for r in rows_all if r.role == "kill" and c["t"] - FEED_BEFORE_S <= r.t_s <= c["t"] + FEED_AFTER_S]
            victims = []
            for r in rows:
                if feed_mod.clean_name(r.victim) not in [feed_mod.clean_name(v.victim) for v in victims]:
                    victims.append(r)
            src = {"ticker": [ln.text for ln in c["lines"]], "cash": cash, "xp": xp}
            delta = _money_delta(money, c["t"], span=max(6.0, keyint * every + 0.5))
            if delta is not None:
                src["money_delta"] = delta
            base = 0.45 + (0.15 if xp else 0) + (0.1 if delta and delta >= 300 else 0)
            if len(victims) >= 2:
                for r in victims:
                    events.append({"kind": "kill", "t": r.t_s if abs(r.t_s - c["t"]) <= FEED_AFTER_S else c["t"],
                                   "subkind": "multikill", "killer": r.killer, "victim": r.victim, "distance": r.distance_m,
                                   "amount": cash, "confidence": min(1.0, base + 0.3), "source": {**src, "feed": r.raw},
                                   "template": c["template"]})
            else:
                r = victims[0] if victims else None
                events.append({"kind": "kill", "t": c["t"], "subkind": "headshot" if any(ln.label == "headshot" for ln in c["lines"]) else "",
                               "killer": r.killer if r else None, "victim": r.victim if r else None,
                               "distance": r.distance_m if r else None, "amount": cash,
                               "confidence": min(1.0, base + (0.3 if r else 0)),
                               "source": {**src, **({"feed": r.raw} if r else {})}, "template": c["template"]})
        elif c["kind"] == "death":
            rows = [r for r in rows_all if r.role == "death" and c["t"] - DEATH_FEED_BEFORE_S <= r.t_s <= c["t"] + keyint]
            r = rows[-1] if rows else None
            src = {"dark_s": round(c["dark_end"] - c["t"], 1)}
            if r:
                src["feed"] = r.raw
                conf = 0.5 + (0.1 if c.get("dark_n", 0) >= 2 else 0) + 0.4
            else:
                # a dark stretch with no feed row: only the respawn text makes it a death (cockpits,
                # bunkers and the vendor menu are dark too)
                if not _respawn_text(path, c["t"] + 0.5, width, height, cfg.detection.ocr_min_conf, respawn_box):
                    logger.debug("[Detect] dark run at %.1fs is not a respawn screen", c["t"])
                    continue
                src["respawn_text"] = True
                conf = 0.6 + (0.1 if c.get("dark_n", 0) >= 2 else 0)
            events.append({"kind": "death", "t": c["t"], "subkind": "downed" if src["dark_s"] > DARK_MAX_S else "",
                           "killer": r.killer if r else None, "victim": r.victim if r else None,
                           "distance": r.distance_m if r else None, "amount": None,
                           "confidence": min(1.0, conf), "template": None, "source": src})
        else:
            cash = next((ln.cash for ln in c["lines"] if ln.cash), None)
            xp = next((ln.xp for ln in c["lines"] if ln.xp), None)
            events.append({"kind": c["kind"], "t": c["t"], "subkind": c["lines"][0].label, "killer": None, "victim": None,
                           "distance": None, "amount": cash, "confidence": 0.6 + (0.1 if xp else 0),
                           "source": {"ticker": [ln.text for ln in c["lines"]], "cash": cash, "xp": xp}, "template": c["template"]})
    # a feed row that says you died but no respawn screen followed: downed (and revived, probably)
    death_ts = [e["t"] for e in events if e["kind"] == "death"]
    for r in rows_all:
        if r.role == "death" and not any(-keyint <= d - r.t_s <= DEATH_FEED_BEFORE_S + keyint for d in death_ts):
            events.append({"kind": "death", "t": r.t_s, "subkind": "downed", "killer": r.killer, "victim": r.victim,
                           "distance": r.distance_m, "amount": None, "confidence": 0.5, "template": None,
                           "source": {"feed": r.raw}})
    # squad kills from the feed are kept as low-key context events
    for r in rows_all:
        if r.role == "squad_kill":
            events.append({"kind": "squad", "t": r.t_s, "subkind": "kill", "killer": r.killer, "victim": r.victim,
                           "distance": r.distance_m, "amount": None, "confidence": 0.5, "source": {"feed": r.raw}, "template": None})
    events.sort(key=lambda e: e["t"])

    # ---- pass 3: refine ----------------------------------------------------------------
    ctx.progress(0.76, f"refining {len(events)} events")
    win = float(cfg.detection.refine_window_s)
    center = refine_center
    for k, ev in enumerate(events):
        try:
            if ev["kind"] in ("kill", "assist", "spotted", "revive") and ev["template"] is not None and ev["template"].size > 400:
                # with the ticker read every Nth keyframe the line may have appeared up to N keyframes earlier
                back = min(win, keyint + 0.5)             # as before at every = 1
                if every > 1:
                    back = max(back, keyint * every + 0.5)
                for t, img in frames.window_pass(path, ev["t"] - back, back + 1.5, rois["ticker"]):
                    if img.shape[0] >= ev["template"].shape[0] and img.shape[1] >= ev["template"].shape[1]:
                        score = float(cv2.matchTemplate(img, ev["template"], cv2.TM_CCOEFF_NORMED).max())
                        if score >= REFINE_MATCH:
                            ev["source"]["refined"] = round(t - ev["t"], 2)
                            ev["t"] = round(t, 3)
                            break
            elif ev["kind"] == "death":
                for t, img in frames.window_pass(path, ev["t"] - keyint - 0.5, keyint + 1.0, center):
                    if float(img.mean()) < DARK_MEAN:
                        ev["source"]["refined"] = round(t - ev["t"], 2)
                        ev["t"] = round(t, 3)
                        break
        except frames.FrameError as e:
            logger.warning("[Detect] refine failed kind=%s t=%.1f: %s", ev["kind"], ev["t"], e)
        ev.pop("template", None)
        if k % 3 == 0:
            ctx.progress(0.76 + 0.14 * (k + 1) / max(1, len(events)), f"refine {k + 1}/{len(events)}")
        if ctx.stopping():
            raise RuntimeError("stopped while refining")

    # ---- evidence crops ----------------------------------------------------------------
    crop_dir = paths.CROPS_DIR / f"rec{rec['id']}"
    crop_dir.mkdir(parents=True, exist_ok=True)
    for old in crop_dir.glob("*.png"):
        try:
            old.unlink()
        except OSError:
            pass
    for ev in events:
        try:
            crops = frames.grab_crops(path, ev["t"] + (0.25 if ev["kind"] != "death" else -0.5), rois)
        except frames.FrameError as e:
            logger.warning("[Detect] crop grab failed t=%.1f: %s", ev["t"], e)
            continue
        stem = f"{ev['kind']}_{int(ev['t'] * 1000):08d}"
        for name in ("ticker", "feed"):
            p = crop_dir / f"{stem}_{name}.png"
            # imgio: cv2.imwrite writes nothing on a non-ASCII data dir (C:\Users\<accented name>); only a crop
            # that is really on disk is recorded (the page then shows "no crop" instead of a 404)
            if imgio.imwrite(p, crops[name]):
                ev[f"crop_{name}"] = f"rec{rec['id']}/{p.name}"
            else:
                logger.warning("[Detect] crop write failed path=%s", p)

    # ---- score + voice ----------------------------------------------------------------
    kill_ts = [e["t"] for e in events if e["kind"] == "kill"]
    death_ts = [e["t"] for e in events if e["kind"] == "death"]
    is_replay = rec.get("kind") == "replay"
    from core import highlights, learning
    facs = learning.factors(ctx.db)      # the user's Keep / False verdicts re-weight look-alike events
    for ev in events:
        t = ev["t"]
        score = {"kill": 1.0, "assist": 0.6, "spotted": 0.4, "revive": 0.3, "death": 0.2, "squad": 0.1}[ev["kind"]]
        if ev["kind"] == "kill":
            others = [x for x in kill_ts if x != t and abs(x - t) <= cfg.highlights.multikill_s]
            score += 0.5 * len(others)
            if any(0 < d - t <= 15 for d in death_ts):
                score += 0.3
        if is_replay and duration - t <= 60:
            score += 0.2
        hits = _keywords_near(ctx.db, rec["id"], t, keywords) if keywords else []
        if hits:
            score += 0.3
            ev["source"]["voice"] = hits
        learning.apply(ev, round(score * ev["confidence"], 3), facs)    # score = base x factor; source.base_score / learned
    # the transcribe stage usually ran first: give its voice events this run's kill / death context
    # (a kill / death the user marked false -- carried over by the write below -- gives none, as
    # in highlights._context on the transcribe side)
    try:
        false_old = [(o["kind"], float(o["t_s"])) for o in ctx.db.query(
            "SELECT kind, t_s FROM events WHERE rec_id=? AND kind IN ('kill', 'death') AND status='false'", (rec["id"],))]
        vctx = [(e["kind"], e["t"]) for e in events if e["kind"] in ("kill", "death")
                and not any(k == e["kind"] and abs(t - e["t"]) <= 1.5 for k, t in false_old)]
        highlights.recontext(ctx.db, rec["id"], vctx, facs)
    except Exception as e:  # noqa: BLE001 - context for voice events must never fail a detect run
        logger.warning("[Detect] voice event context failed rec=%s err=%s", rec["id"], e)

    # ---- write ------------------------------------------------------------------------
    now = time.time()
    con = ctx.db.connect()
    con.execute("BEGIN IMMEDIATE")
    try:
        # read the old rows under the write lock: a Keep / False positive clicked while detect ran is not lost
        old = [dict(r) for r in con.execute("SELECT * FROM events WHERE rec_id=?", (rec["id"],)).fetchall()]
        # manual marks are the user's; voice events belong to the transcribe stage
        con.execute("DELETE FROM events WHERE rec_id=? AND kind NOT IN ('manual', 'voice')", (rec["id"],))
        con.execute("DELETE FROM ticker_lines WHERE rec_id=?", (rec["id"],))
        con.execute("DELETE FROM feed_rows WHERE rec_id=?", (rec["id"],))
        prevs = carry_matches(events, old)
        for k, ev in enumerate(events):
            prev = prevs.get(k)
            status = prev["status"] if prev else "new"
            note = prev["note"] if prev else ""
            marker = prev["resolve_marker"] if prev else None
            # the user's clip in / out move with the event (they were set relative to the old t)
            shift = ev["t"] - prev["t_s"] if prev else 0.0
            clip_in = prev["clip_in"] + shift if prev and prev.get("clip_in") is not None else None
            clip_out = prev["clip_out"] + shift if prev and prev.get("clip_out") is not None else None
            con.execute(
                "INSERT INTO events (rec_id, t_s, t_end_s, kind, subkind, score, confidence, killer, victim, weapon, amount,"
                " source, crop_ticker, crop_feed, status, resolve_marker, note, clip_in, clip_out, created_utc)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec["id"], ev["t"], None, ev["kind"], ev.get("subkind") or "", ev["score"], round(ev["confidence"], 3),
                 ev.get("killer"), ev.get("victim"), None, ev.get("amount"),
                 json.dumps({**ev["source"], "auto": True, **({"distance_m": ev["distance"]} if ev.get("distance") else {})}),
                 ev.get("crop_ticker"), ev.get("crop_feed"), status, marker, note, clip_in, clip_out, now))
        con.executemany(
            "INSERT INTO ticker_lines (rec_id, t_s, text, label, cash, xp, conf, created_utc) VALUES (?,?,?,?,?,?,?,?)",
            [(rec["id"], ln.t_s, ln.text, ln.label, ln.cash, ln.xp, ln.conf, now) for ln in sightings])
        con.executemany(
            "INSERT INTO feed_rows (rec_id, t_s, killer, victim, distance_m, role, raw, conf, created_utc) VALUES (?,?,?,?,?,?,?,?,?)",
            [(rec["id"], r.t_s, r.killer, r.victim, r.distance_m, r.role, r.raw, r.conf, now) for r in rows_all])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    checkpoints.clear("detect", rec["id"])
    counts = {}
    for ev in events:
        counts[ev["kind"]] = counts.get(ev["kind"], 0) + 1
    from core.stages import STAGE_SQL, stage_params
    ctx.db.execute(f"UPDATE recordings SET hud_profile=?, {STAGE_SQL}, updated_utc=? WHERE id=?",
                   (profile, *stage_params("detect", "done", counts=counts, profile=profile, keyframes=n_frames,
                                           ocr_engine="fast" if tick_ocr is ocr_fast else "full", ticker_reused=n_reused,
                                           ocr_gpu=oc.gpu_calls, ocr_gpu_off=_gpu_off(), ticker_every=every,
                                           template_id=tpl["id"] if tpl else None, zones_source=zsource,
                                           resumed_at=round(resumed_at, 1) if resumed_at is not None else None), now, rec["id"]))
    fresh = ctx.db.one("SELECT * FROM recordings WHERE id=?", (rec["id"],))
    ctx.bus.publish("events", action="detected", rec_id=rec["id"], counts=counts)
    ctx.bus.publish("recording", action="updated", rec=rec_to_dict(fresh))
    ctx.progress(1.0, "done")
    elapsed = time.monotonic() - t_start
    return (f"{counts.get('kill', 0)} kills, {counts.get('assist', 0)} assists, {counts.get('death', 0)} deaths,"
            f" {counts.get('spotted', 0)} spotted, {counts.get('squad', 0)} squad | {n_frames} keyframes, {len(sightings)} ticker lines,"
            f" {len(rows_all)} feed rows, ocr {oc.calls}x {oc.seconds:.0f}s ({oc.gpu_calls} on the GPU{_gpu_off_note()})"
            f" (ticker reused {n_reused}x) | {elapsed:.0f}s ({duration / max(elapsed, 0.01):.0f}x)")


def carry_matches(events: list[dict], old: list[dict], window_s: float = 1.5) -> dict[int, dict]:
    """Which old row each new event inherits the user's status / note / marker / clip bounds
    from: same kind within window_s, closest pairs first, every old row used at most once
    (a multikill gives two kills at the same t -- each keeps its own verdict)."""
    pairs = []
    for i, ev in enumerate(events):
        for j, o in enumerate(old):
            if o["kind"] == ev["kind"]:
                dt = abs(float(o["t_s"]) - float(ev["t"]))
                if dt <= window_s:
                    pairs.append((dt, i, j))
    out: dict[int, dict] = {}
    used: set[int] = set()
    for _dt, i, j in sorted(pairs):
        if i in out or j in used:
            continue
        out[i] = old[j]
        used.add(j)
    return out


def _money_delta(money: list[tuple[float, int | None, int | None]], t: float, span: float = 6.0) -> int | None:
    before = [m for m in money if t - span <= m[0] < t]
    after = [m for m in money if t <= m[0] <= t + span]
    if not before or not after:
        return None
    a0, a1 = before[-1][2], after[-1][2]
    if a0 is None or a1 is None:
        return None
    return a1 - a0


_TEXT_RE = re.compile(r"\s+")


def ticker_summary(db, rec_id: int | None = None) -> list[dict]:
    """Raw ticker texts seen, grouped (calibration page)."""
    where = "WHERE rec_id=?" if rec_id else ""
    params = (rec_id,) if rec_id else ()
    rows = db.query(f"SELECT text, label, cash, xp FROM ticker_lines {where}", params)
    groups: dict[str, dict] = {}
    for r in rows:
        key = _TEXT_RE.sub(" ", re.sub(r"\d[\d,.]*", "#", ticker_mod.normalize(r["text"]))).strip()
        g = groups.setdefault(key, {"pattern": key, "label": r["label"], "n": 0, "examples": [], "cash": set(), "xp": set()})
        g["n"] += 1
        if r["text"] not in g["examples"] and len(g["examples"]) < 3:
            g["examples"].append(r["text"])
        if r["cash"]:
            g["cash"].add(r["cash"])
        if r["xp"]:
            g["xp"].add(r["xp"])
    out = []
    for g in groups.values():
        g["cash"] = sorted(g["cash"])[:6]
        g["xp"] = sorted(g["xp"])[:6]
        out.append(g)
    out.sort(key=lambda g: -g["n"])
    return out


def names_summary(db, rec_id: int | None = None) -> list[dict]:
    """Names seen with a distance tag (= involving the player) -- the alias candidates."""
    where = "WHERE distance_m IS NOT NULL" + (" AND rec_id=?" if rec_id else "")
    params = (rec_id,) if rec_id else ()
    rows = db.query(f"SELECT killer, victim, role FROM feed_rows {where}", params)
    names: dict[str, dict] = {}
    for r in rows:
        for side, name in (("killer", r["killer"]), ("victim", r["victim"])):
            key = feed_mod.clean_name(name)
            if not key:
                continue
            g = names.setdefault(key, {"name": name, "n": 0, "as_killer": 0, "as_victim": 0})
            g["n"] += 1
            g["as_killer" if side == "killer" else "as_victim"] += 1
    return sorted(names.values(), key=lambda g: -g["n"])
