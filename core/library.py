"""The footage library: walk the configured roots, keep one row per recording, notice
files that moved (e.g. from a fast drive to an archive drive) or vanished, skip files OBS is still writing, and queue a
probe for anything new.

Identity is sha1(name|size): the bytes do not change when a file is moved between
drives, so the row (and later its events and transcript) follows the file.
"""

import fnmatch
import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from core import settings as settings_mod

logger = logging.getLogger("wclips.library")

# OBS names: "Replay_2026-10-16_06-17-26.mp4" (replay buffer save, END of the buffer) and
# "2026-10-10_14-05-16.mp4" (plain recording, START). Stream VODs (media/vods.py):
# "VOD_Twitch_2026-11-05_20-14-03_2612345678.mp4" (the stream's START).
_NAME_RE = re.compile(r"^(?:(?P<replay>Replay_)|(?P<vod>VOD_[A-Za-z]+_))?(?P<ts>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})")


def parse_name(name: str) -> tuple[str, float | None]:
    m = _NAME_RE.match(name)
    if not m:
        return "recording", None
    try:
        ts = datetime.strptime(m.group("ts"), "%Y-%m-%d_%H-%M-%S").timestamp()
    except ValueError:
        ts = None
    return ("replay" if m.group("replay") else "vod" if m.group("vod") else "recording"), ts


def _canon_key(k) -> str:
    from media.vods import canon_channel_key      # lazy: media.vods imports this module
    return canon_channel_key(k)


def _vod_fields(path: Path) -> dict:
    """A stream VOD's .vod.json sidecar (media/vods.py) as recordings columns: title, source_url, channel,
    channel_key (the streamer key zone templates / favorites use), channel_url; _start = the stream start."""
    try:
        side = json.loads(path.with_suffix(".vod.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    out = {"title": side.get("title") or None, "source_url": side.get("url") or None,
           "channel": side.get("channel") or None, "channel_key": _canon_key(side.get("channel_key")) or None,
           "channel_url": side.get("channel_url") or None}
    try:
        out["_start"] = float(side["start_utc"]) if side.get("start_utc") else None
    except (TypeError, ValueError):
        out["_start"] = None
    return out


def vod_provider(name: str) -> str | None:
    """'VOD_Twitch_...' -> 'Twitch'."""
    m = re.match(r"^VOD_([A-Za-z]+)_\d{4}-", name or "")
    return m.group(1) if m else None


def identity_key(name: str, size: int) -> str:
    return hashlib.sha1(f"{name}|{size}".encode("utf-8")).hexdigest()[:16]


def _priority(name_utc: float | None, mtime: float) -> int:
    """Newest footage first: the queue orders by priority DESC, so the recording time (minutes)
    is the priority. Last night's session is probed before August's."""
    return int((name_utc or mtime or 0) // 60)


def _drive(path: str) -> str:
    d = os.path.splitdrive(path)[0]
    return d.upper() if d else (path.split(os.sep)[0] if path.startswith("\\\\") else "")


def rec_to_dict(row: dict) -> dict:
    out = dict(row)
    for k in ("audio_tracks", "stages"):
        try:
            out[k] = json.loads(out.get(k) or ("[]" if k == "audio_tracks" else "{}"))
        except json.JSONDecodeError:
            out[k] = [] if k == "audio_tracks" else {}
    out["missing"] = bool(out.get("missing"))
    return out


class Library:
    def __init__(self, cfg, db, bus, enqueue_probe=None):
        self.cfg = cfg
        self.db = db
        self.bus = bus
        self.enqueue_probe = enqueue_probe       # callable(rec_id) -> job id, set by the app
        self.last_scan: dict | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- scanning -----------------------------------------------------------------
    def scan(self) -> dict:
        """Walk every root once. Returns a summary and publishes it on the bus."""
        with self._lock:
            return self._scan()

    def _scan(self) -> dict:
        t0 = time.time()
        pats = settings_mod.patterns(self.cfg)
        lib = self.cfg.library
        summary = {"started_utc": t0, "roots": [], "found": 0, "new": 0, "moved": 0, "duplicates": 0,
                   "missing": 0, "returned": 0, "growing": 0, "queued": 0}
        seen_keys: set[str] = set()
        for root in lib.roots:
            rp = Path(root)
            entry = {"root": root, "ok": rp.is_dir(), "files": 0}
            summary["roots"].append(entry)
            if not entry["ok"]:
                logger.warning("[Library] root not reachable root=%s", root)
                continue
            for path in self._walk(rp, pats, lib.recursive):
                try:
                    st = path.stat()
                except OSError:
                    continue
                entry["files"] += 1
                summary["found"] += 1
                if lib.min_age_s and (t0 - st.st_mtime) < lib.min_age_s:
                    summary["growing"] += 1
                    logger.debug("[Library] still being written path=%s", path)
                    continue
                key = identity_key(path.name, st.st_size)
                if key in seen_keys:
                    summary["duplicates"] += 1
                    continue
                seen_keys.add(key)
                self._upsert(key, path, st, t0, summary)
        # anything we know that was not seen this pass (and whose root was reachable) is missing
        ok_roots = [e["root"].lower() for e in summary["roots"] if e["ok"]]
        for row in self.db.query("SELECT id, key, path, missing FROM recordings WHERE last_seen_utc < ?", (t0,)):
            under_ok_root = any(row["path"].lower().startswith(r) for r in ok_roots)
            if not under_ok_root and not os.path.exists(row["path"]):
                continue                        # its drive is offline: leave the row as it was
            if not os.path.exists(row["path"]) and row["key"] not in seen_keys and not row["missing"]:
                self.db.execute("UPDATE recordings SET missing=1, updated_utc=? WHERE id=?", (t0, row["id"]))
                summary["missing"] += 1
                logger.info("[Library] missing path=%s", row["path"])
        summary["elapsed_s"] = round(time.time() - t0, 2)
        self.last_scan = summary
        # the watcher scans every minute: only a pass that changed something is news (an unchanged
        # pass would re-render the page's detail card and wipe a half-typed note)
        changed = any(summary.get(k) for k in ("new", "moved", "missing", "returned", "queued"))
        logger.log(logging.INFO if changed else logging.DEBUG,
                   "[Library] scanned found=%d new=%d moved=%d missing=%d growing=%d dup=%d in %.1fs",
                   summary["found"], summary["new"], summary["moved"], summary["missing"],
                   summary["growing"], summary["duplicates"], summary["elapsed_s"])
        if changed:
            self.bus.publish("library", action="scanned", summary=summary)
        return summary

    @staticmethod
    def _walk(root: Path, pats: list[str], recursive: bool):
        if recursive:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith((".", "_"))]
                for fn in filenames:
                    if any(fnmatch.fnmatch(fn.lower(), p) for p in pats):
                        yield Path(dirpath) / fn
        else:
            for p in root.iterdir():
                if p.is_file() and any(fnmatch.fnmatch(p.name.lower(), pat) for pat in pats):
                    yield p

    def _upsert(self, key: str, path: Path, st, now: float, summary: dict) -> None:
        row = self.db.one("SELECT id, path, missing, status FROM recordings WHERE key=?", (key,))
        spath = str(path)
        if row is None:
            kind, name_utc = parse_name(path.name)
            vod = {}
            if kind == "vod":                           # title / link / streamer from the download's sidecar
                vod = _vod_fields(path)
                if vod.get("_start"):
                    name_utc = vod["_start"]             # exact (the name is to the second, local time)
            rec_id = self.db.execute(
                "INSERT INTO recordings (key, path, name, drive, kind, name_utc, size, mtime, created_utc, updated_utc,"
                " last_seen_utc, title, source_url, channel, channel_key, channel_url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, spath, path.name, _drive(spath), kind, name_utc, st.st_size, st.st_mtime, now, now, now,
                 vod.get("title"), vod.get("source_url"), vod.get("channel"), vod.get("channel_key"), vod.get("channel_url")))
            summary["new"] += 1
            logger.info("[Library] new path=%s size=%d", spath, st.st_size)
            if self.enqueue_probe is not None and self.enqueue_probe(rec_id, _priority(name_utc, st.st_mtime)) is not None:
                summary["queued"] += 1
            return
        updates = {"last_seen_utc": now, "size": st.st_size, "mtime": st.st_mtime}
        if path.name.startswith("VOD_"):
            # a VOD row made before its sidecar was readable (or before streamer keys existed): fill it once it is
            have = self.db.one("SELECT title, channel_key FROM recordings WHERE id=?", (row["id"],))
            if have is not None and (not have["title"] or not have["channel_key"]):
                vod = _vod_fields(path)
                for k in ("title", "source_url", "channel", "channel_key", "channel_url"):
                    if vod.get(k) and not (have["title"] if k == "title" else None):
                        updates[k] = vod[k]
        if row["missing"]:
            updates["missing"] = 0
            summary["returned"] += 1
        if row["path"] != spath:
            if os.path.exists(row["path"]):
                summary["duplicates"] += 1
                logger.info("[Library] same recording at two paths keep=%s other=%s", row["path"], spath)
            else:
                updates["path"] = spath
                updates["drive"] = _drive(spath)
                summary["moved"] += 1
                logger.info("[Library] moved from=%s to=%s", row["path"], spath)
        updates["updated_utc"] = now
        sets = ", ".join(f"{k}=?" for k in updates)
        self.db.execute(f"UPDATE recordings SET {sets} WHERE id=?", (*updates.values(), row["id"]))
        if row["status"] == "new" and self.enqueue_probe is not None:
            kind, name_utc = parse_name(path.name)
            if self.enqueue_probe(row["id"], _priority(name_utc, st.st_mtime)) is not None:
                summary["queued"] += 1

    # -- queries ------------------------------------------------------------------
    def summary(self) -> dict:
        rows = self.db.query(
            "SELECT drive, COUNT(*) n, SUM(size) bytes, SUM(COALESCE(duration_s,0)) secs,"
            " SUM(CASE WHEN status='probed' THEN 1 ELSE 0 END) probed,"
            " SUM(missing) missing FROM recordings GROUP BY drive ORDER BY drive")
        total = {"files": 0, "bytes": 0, "secs": 0.0, "probed": 0, "missing": 0}
        for r in rows:
            total["files"] += r["n"]
            total["bytes"] += r["bytes"] or 0
            total["secs"] += r["secs"] or 0
            total["probed"] += r["probed"] or 0
            total["missing"] += r["missing"] or 0
        return {"drives": rows, "total": total, "last_scan": self.last_scan}

    def list(self, drive: str = "", q: str = "", status: str = "", limit: int = 500) -> list[dict]:
        where, params = [], []
        if drive:
            where.append("drive=?")
            params.append(drive.upper())
        if q:
            where.append("(name LIKE ? OR title LIKE ?)")         # a stream VOD by its title too
            params += [f"%{q}%", f"%{q}%"]
        if status == "missing":
            where.append("missing=1")
        elif status:
            where.append("status=? AND missing=0")
            params.append(status)
        sql = ("SELECT recordings.*, (SELECT COUNT(*) FROM transcript_segments s WHERE s.rec_id = recordings.id) n_segments,"
               " (SELECT COUNT(*) FROM events e WHERE e.rec_id = recordings.id AND e.kind='kill' AND e.status != 'false') n_kills,"
               " (SELECT COUNT(*) FROM events e WHERE e.rec_id = recordings.id AND e.kind='death' AND e.status != 'false') n_deaths,"
               " (SELECT COUNT(*) FROM events e WHERE e.rec_id = recordings.id AND e.status != 'false') n_events"
               " FROM recordings")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(name_utc, mtime) DESC LIMIT ?"
        params.append(max(1, min(limit, 5000)))
        return [rec_to_dict(r) for r in self.db.query(sql, params)]

    def get(self, rec_id: int) -> dict | None:
        row = self.db.one("SELECT * FROM recordings WHERE id=?", (rec_id,))
        if row is None:
            return None
        out = rec_to_dict(row)
        out["tracks"] = self.db.query("SELECT * FROM tracks WHERE rec_id=? ORDER BY idx", (rec_id,))
        out["jobs"] = self.db.query("SELECT * FROM jobs WHERE rec_id=? ORDER BY id DESC LIMIT 20", (rec_id,))
        out["event_counts"] = {r["kind"]: r["n"] for r in self.db.query(
            "SELECT kind, COUNT(*) n FROM events WHERE rec_id=? AND status!='false' GROUP BY kind", (rec_id,))}
        from core import transcripts
        out["transcript"] = transcripts.summary(self.db, rec_id)
        out["n_segments"] = out["transcript"]["n"]
        ec = out["event_counts"]
        out["n_kills"] = ec.get("kill", 0)
        out["n_deaths"] = ec.get("death", 0)
        out["n_events"] = sum(ec.values())
        return out

    # -- watcher ------------------------------------------------------------------
    def start_watch(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch_loop, name="library-watch", daemon=True)
        self._thread.start()

    def stop_watch(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _watch_loop(self) -> None:
        while not self._stop.wait(self.cfg.library.watch_s):
            if not self.cfg.library.auto_scan_new:
                continue
            try:
                self.scan()
            except Exception:
                logger.warning("[Library] watch scan failed", exc_info=True)
