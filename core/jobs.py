"""Stage queue: one row per (recording, stage), worked by lane threads.

Lanes: `io` (probe, thumbnails: cheap, never limited), `video` (NVDEC / NVENC: detect, proxy,
prep, refine) and `stt` (whisper: one model resident, its own fixed thread count).

Drive-aware parallelism: a footage-heavy job (video or stt lane) takes a *slot*
on the drive its recording lives on. Each drive has ``core.drives.slots()`` of them per stage
(from the drive's read benchmark: an NVMe SSD feeds several keyframe passes, a SATA HDD
about one), and video-lane jobs share a global GPU cap (``settings.jobs.gpu_parallel``). The
claim walks the queue in priority order and hands out the first job whose drive (and the GPU)
has a free slot, so a full HDD never blocks work on an SSD. Slots are counted in memory under a
condition variable; ``_run`` releases them in its finally. Code outside the queue (e.g. a clip
export) takes a slot with ``with queue.drive_slot(drive, stage):``.

A job is claimed with BEGIN IMMEDIATE so two lanes never take the same row; a crash leaves it
'running' and ``recover()`` re-queues it at startup. Pause is a settings flag the workers poll.
``run_pending()`` drains synchronously on the calling thread (tests); ``drain()`` uses the
threaded workers and returns when the lanes are empty (headless scan).
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import threading
import time
import traceback
from collections import Counter

from core import drives as drives_mod
from utils.logging_config import set_context

logger = logging.getLogger("wclips.jobs")

LANES = ("io", "video", "stt", "net")       # net: VOD downloads (media/vods.py) -- network-bound, no drive slot
REORDER_HEADROOM = 100_000           # minutes: a dragged order sits above enqueues for footage up to ~69 days in the future
STAGE_LANES = {
    "probe": "io",
    "prep": "video",        # one decode pass -> proxy + HUD crops + voice wav
    "detect": "video",
    "refine": "video",
    "transcribe": "stt",
    "thumbs": "io",
    "proxy": "video",       # browser proxy of the whole recording
    "download": "net",      # a stream VOD (yt-dlp) into the provider folder; jobs.arg = the import as JSON
}
SLOT_STAGES = ("detect", "proxy", "transcribe")      # the stages the UI shows per-drive slots for


class SlotBusy(RuntimeError):
    """drive_slot(wait=False) / timeout: no free slot on that drive (or the GPU)."""


class JobContext:
    """What a stage handler gets: the job row, the recording row, settings, db, bus and a
    progress reporter (throttled to twice a second)."""

    def __init__(self, queue: "JobQueue", job: dict, rec: dict | None):
        self.queue = queue
        self.job = job
        self.rec = rec
        self.cfg = queue.cfg
        self.db = queue.db
        self.bus = queue.bus
        self.drive = (rec or {}).get("drive") or ""
        self._last = 0.0

    def progress(self, frac: float, message: str = "") -> None:
        now = time.monotonic()
        if frac < 1.0 and now - self._last < 0.5:
            return
        self._last = now
        frac = max(0.0, min(1.0, float(frac)))
        self.db.execute("UPDATE jobs SET progress=?, message=? WHERE id=?", (frac, message[:200], self.job["id"]))
        self.bus.publish("job", action="progress", job={"id": self.job["id"], "rec_id": self.job["rec_id"],
                                                        "stage": self.job["stage"], "progress": frac,
                                                        "message": message[:200]})

    def stopping(self) -> bool:
        """The app is shutting down, or Stop was pressed on this job (POST /api/jobs/{id}/stop)."""
        ev = self.queue._cancel.get(self.job["id"])
        return self.queue._stop.is_set() or bool(ev and ev.is_set())

    def release_drive(self) -> None:
        """The heavy read is over (e.g. transcribe after the voice wav is extracted): free this
        job's drive slot early. The GPU share, if any, is kept until the job ends."""
        self.queue._release_drive(("job", self.job["id"]))


class JobQueue:
    def __init__(self, cfg, db, bus):
        self.cfg = cfg
        self.db = db
        self.bus = bus
        self.handlers: dict[str, callable] = {}
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._claim_lock = threading.Lock()
        self._running: dict[int, dict] = {}
        self._cancel: dict[int, threading.Event] = {}  # job id -> Stop pressed (a running job's handler polls it)
        # slot accounting (all under _slots)
        self._slots = threading.Condition()
        self._held: dict[tuple, dict] = {}          # token -> {drive, stage, lane, gpu, job_id, rec_id, since}
        self._bench_busy: set[str] = set()          # drives being benchmarked: no heavy claims there
        self._bench_wanted: set[str] = set()        # drives waiting to be benchmarked: no new heavy claims
        self._tokens = itertools.count(1)
        self._tls = threading.local()               # .slot = (drive, stage) held by this thread's job
        self._no_claim = False                      # drain() after Ctrl+C: finish what runs, start nothing
        self._ignore_pause = False
        self._stats_lock = threading.Lock()
        self.finished: Counter = Counter()          # (stage, state) -> n, for drain() summaries
        self.poll_s = 2.0
        self.drives = drives_mod.DriveManager(cfg, db, bus, self)

    @property
    def running(self) -> dict[int, dict]:
        """Snapshot {job id: job} of the jobs running now (safe to iterate from any thread)."""
        return dict(self._running)

    # -- registration / enqueue ----------------------------------------------------
    def register(self, stage: str, fn) -> None:
        self.handlers[stage] = fn

    def enqueue(self, rec_id: int | None, stage: str, priority: int = 0, arg: str | None = None,
                after_running: bool = False) -> int | None:
        """Queue a stage for a recording (or, rec_id None, a job described by `arg` -- a download).
        Returns the job id, or None when the same stage is already queued / running for it (dedupe,
        on the arg too). after_running=True: only a QUEUED twin dedupes -- with one running, a new job
        is queued to run after it (the claim never starts a twin of a running job); for a re-run whose
        inputs changed while the current run was already going (zones re-detect)."""
        lane = STAGE_LANES.get(stage, "io")
        states = "('queued')" if after_running else "('queued','running')"
        dup = self.db.one(f"SELECT id FROM jobs WHERE rec_id IS ? AND stage=? AND arg IS ? AND state IN {states}",
                          (rec_id, stage, arg))
        if dup is not None:
            return None
        jid = self.db.execute(
            "INSERT INTO jobs (rec_id, stage, lane, state, priority, created_utc, arg) VALUES (?,?,?,'queued',?,?,?)",
            (rec_id, stage, lane, priority, time.time(), arg))
        self.bus.publish("job", action="queued", job={"id": jid, "rec_id": rec_id, "stage": stage, "lane": lane})
        self._wake.set()
        return jid

    def recover(self) -> int:
        """Jobs left 'running' by a crash go back to the queue."""
        n = self.db.execute("UPDATE jobs SET state='queued', progress=0, message='recovered', started_utc=NULL"
                            " WHERE state='running'")
        if n:
            logger.info("[Jobs] recovered running jobs n=%d", n)
        return n

    # -- control ------------------------------------------------------------------
    @property
    def paused(self) -> bool:
        return bool(self.cfg.jobs.paused)

    def set_paused(self, paused: bool) -> None:
        self.cfg.jobs.paused = bool(paused)
        self.bus.publish("jobs", action="paused" if paused else "resumed")
        if not paused:
            self._wake.set()

    def retry(self, job_id: int) -> bool:
        n = self.db.execute("UPDATE jobs SET state='queued', progress=0, message='', error=NULL, started_utc=NULL,"
                            " finished_utc=NULL WHERE id=? AND state IN ('failed','skipped','done')", (job_id,))
        if n:
            self.bus.publish("job", action="queued", job={"id": job_id})
            self._wake.set()
        return bool(n)

    def stop_job(self, job_id: int) -> bool:
        """Stop a RUNNING job: its handler sees ctx.stopping() and ends; the job is marked skipped
        ('stopped by you'). Only handlers that poll stopping() react (download, proxy)."""
        ev = self._cancel.get(job_id)
        if ev is None:
            return False
        ev.set()
        self.bus.publish("job", action="stopping", job={"id": job_id})
        return True

    def skip(self, job_id: int) -> bool:
        n = self.db.execute("UPDATE jobs SET state='skipped', finished_utc=? WHERE id=? AND state='queued'",
                            (time.time(), job_id))
        if n:
            self.bus.publish("job", action="skipped", job={"id": job_id})
        return bool(n)

    def clear(self, states=("done", "failed", "skipped")) -> int:
        """Delete jobs in `states`. 'running' is never cleared: those finish (or fail) on their own."""
        states = tuple(s for s in states if s != "running")
        if not states:
            return 0
        marks = ",".join("?" for _ in states)
        n = self.db.execute(f"DELETE FROM jobs WHERE state IN ({marks})", states)
        if n:
            self.bus.publish("jobs", action="cleared", n=n)
        return n

    def reorder(self, ids: list[int]) -> int:
        """Put queued jobs in the given order (first = runs first; the queue claims by priority
        DESC, id ASC within a lane). The listed jobs go above every queued job NOT listed (the page
        may show only part of a long queue), keeping those in their order below. Ids that are no
        longer queued are ignored."""
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        n = 0
        con = self.db.connect()
        marks = ",".join("?" for _ in ids)
        with self._claim_lock:
            con.execute("BEGIN IMMEDIATE")
            try:
                top = con.execute(f"SELECT MAX(priority) FROM jobs WHERE state='queued' AND id NOT IN ({marks})",
                                  tuple(ids)).fetchone()[0]
                # above the unlisted queued jobs AND above what a new enqueue gets (epoch minutes of the
                # recording, + small bumps), so the hand-made order is not overtaken by the next follow-up
                base = max(int(top or 0), int(time.time() // 60) + REORDER_HEADROOM) + 1
                for pos, jid in enumerate(ids):
                    n += con.execute("UPDATE jobs SET priority=? WHERE id=? AND state='queued'",
                                     (base + len(ids) - 1 - pos, jid)).rowcount
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        if n:
            self.bus.publish("jobs", action="reordered", n=n)
            self._wake.set()
        return n

    def counts(self) -> dict:
        out = {"queued": 0, "running": 0, "done": 0, "failed": 0, "skipped": 0}
        for r in self.db.query("SELECT state, COUNT(*) n FROM jobs GROUP BY state"):
            out[r["state"]] = r["n"]
        out["paused"] = self.paused
        return out

    def list(self, state: str = "", limit: int = 200) -> list[dict]:
        if state:
            rows = self.db.query("SELECT j.*, r.name rec_name FROM jobs j LEFT JOIN recordings r ON r.id=j.rec_id"
                                 " WHERE j.state=? ORDER BY j.id DESC LIMIT ?", (state, limit))
        else:
            rows = self.db.query("SELECT j.*, r.name rec_name FROM jobs j LEFT JOIN recordings r ON r.id=j.rec_id"
                                 " ORDER BY CASE j.state WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,"
                                 # queued rows in the order they will be claimed (priority DESC, id ASC)
                                 " CASE WHEN j.state='queued' THEN -j.priority END,"
                                 " CASE WHEN j.state='queued' THEN j.id END ASC,"
                                 " j.id DESC LIMIT ?", (limit,))
        return rows

    # -- slots --------------------------------------------------------------------
    def lane_threads(self) -> dict[str, int]:
        """Worker threads per lane. The video lane gets one per GPU share (the drive slots and the
        GPU cap decide how many actually run); stt stays at its own count."""
        j = self.cfg.jobs
        return {"io": max(1, j.io_lanes), "video": max(1, j.video_lanes, j.gpu_parallel), "stt": max(1, j.stt_lanes),
                "net": max(1, int(getattr(self.cfg.vods, "parallel", 1)))}

    def _gpu_held(self) -> int:
        return sum(1 for e in self._held.values() if e["gpu"])

    def _drive_held(self, drive: str) -> int:
        return sum(1 for e in self._held.values() if e["drive"] and e["drive"] == drive)

    def _refusal(self, drive: str, stage: str, lane: str, bitrate: float | None, gpu: bool | None = None) -> str | None:
        """Why a job of this stage on this drive cannot start now (None = it can). Under _slots.
        gpu: whether it needs a GPU share (default: video lane)."""
        need_gpu = (lane == "video") if gpu is None else gpu
        if need_gpu and self._gpu_held() >= max(1, int(self.cfg.jobs.gpu_parallel)):
            return "gpu"
        if lane in drives_mod.HEAVY_LANES and drive:
            if drive in self._bench_busy or drive in self._bench_wanted:
                return "bench"
            if self._drive_held(drive) >= self.drives.slots(drive, stage, bitrate):
                return "drive"
            # bandwidth budget (auto mode): what already runs on the drive plus this job must fit in
            # what the benchmark measured. slots() alone is per job, so low-bitrate files (several slots
            # each) could otherwise join a high-bitrate one on an HDD that only feeds one.
            if str(self.cfg.jobs.parallel).strip().lower() == "auto":
                agg = drives_mod.aggregate_mb_s(self.drives.latest(drive))
                used = sum(e.get("demand", 0.0) for e in self._held.values() if e["drive"] == drive)
                if agg and used > 0 and used + drives_mod.demand_mb_s(stage, bitrate) > agg:
                    return "bandwidth"
        return None

    def _reserve(self, token: tuple, drive: str, stage: str, lane: str, job_id=None, rec_id=None,
                 gpu: bool | None = None, bitrate: float | None = None) -> None:
        heavy = lane in drives_mod.HEAVY_LANES
        self._held[token] = {"drive": drive if (heavy and drive) else None, "stage": stage, "lane": lane,
                             "demand": drives_mod.demand_mb_s(stage, bitrate) if (heavy and drive) else 0.0,
                             "gpu": (lane == "video") if gpu is None else bool(gpu),
                             "job_id": job_id, "rec_id": rec_id, "since": time.time(),
                             "thread": threading.current_thread().name}

    def _release(self, token: tuple) -> None:
        with self._slots:
            if self._held.pop(token, None) is not None:
                self._slots.notify_all()
        self._wake.set()

    def _release_drive(self, token: tuple) -> None:
        with self._slots:
            e = self._held.get(token)
            if e is not None and e["drive"]:
                e["drive"] = None
                self._slots.notify_all()
        cur = getattr(self._tls, "slot", None)
        if cur is not None:
            self._tls.slot = None
        self._wake.set()

    @contextlib.contextmanager
    def drive_slot(self, drive: str, stage: str = "proxy", wait: bool = True, timeout: float | None = None,
                   bitrate: float | None = None):
        """Hold one slot of `drive` (a drive letter or any path on it) for `stage` while the block
        runs; video stages also take a GPU share. Re-entrant: inside a job that already holds a slot
        on that drive it is a no-op. wait=False / timeout -> SlotBusy when nothing is free."""
        d = drives_mod.norm_drive(drive)
        cur = getattr(self._tls, "slot", None)
        if cur is not None and cur[0] == d:
            yield {"drive": d, "stage": stage, "reentrant": True}
            return
        lane = STAGE_LANES.get(stage, "video")
        # a thread that already holds a GPU share (its video job, or an outer drive_slot) does not
        # take a second one: with gpu_parallel=1 that would wait on itself forever
        own_gpu = getattr(self._tls, "gpu", 0) > 0
        need_gpu = lane == "video" and not own_gpu
        token = ("slot", next(self._tokens))
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        with self._slots:
            while True:
                why = self._refusal(d, stage, lane, bitrate, gpu=need_gpu)
                if why is None:
                    self._reserve(token, d, stage, lane, gpu=need_gpu, bitrate=bitrate)
                    break
                left = None if deadline is None else deadline - time.monotonic()
                if not wait or (left is not None and left <= 0) or self._stop.is_set():
                    raise SlotBusy(f"no free {why} slot for {stage} on {d or '-'}")
                self._slots.wait(0.5 if left is None else min(0.5, left))
        prev = cur
        prev_gpu = getattr(self._tls, "gpu", 0)
        self._tls.slot = (d, stage)
        self._tls.gpu = prev_gpu + (1 if need_gpu else 0)
        try:
            yield {"drive": d, "stage": stage, "reentrant": False}
        finally:
            self._tls.slot = prev
            self._tls.gpu = prev_gpu
            self._release(token)

    # -- benchmark exclusivity (core/drives.py) ---------------------------------------
    def drive_busy(self, drive: str) -> bool:
        with self._slots:
            return self._drive_held(drives_mod.norm_drive(drive)) > 0

    def bench_begin(self, drive: str, wait_s: float = 0.0) -> bool:
        """Reserve `drive` for a benchmark: True once no footage-heavy job runs on it. While this
        waits (wait_s > 0) no new heavy job starts there. False when it stayed busy."""
        d = drives_mod.norm_drive(drive)
        deadline = time.monotonic() + max(0.0, wait_s)
        with self._slots:
            if wait_s > 0:
                self._bench_wanted.add(d)
            try:
                while self._drive_held(d) > 0 or d in self._bench_busy:
                    left = deadline - time.monotonic()
                    if left <= 0 or self._stop.is_set():
                        return False
                    self._slots.wait(min(0.5, left))
                self._bench_busy.add(d)
                return True
            finally:
                self._bench_wanted.discard(d)

    def bench_end(self, drive: str) -> None:
        with self._slots:
            self._bench_busy.discard(drives_mod.norm_drive(drive))
            self._slots.notify_all()
        self._wake.set()

    # -- status for the UI -----------------------------------------------------------
    def capacity(self) -> dict:
        j = self.cfg.jobs
        with self._slots:
            gpu = self._gpu_held()
            held = Counter(e["drive"] for e in self._held.values() if e["drive"])
        out = {"mode": j.parallel, "gpu_parallel": j.gpu_parallel, "gpu_running": gpu, "max_parallel": j.max_parallel,
               "auto_bench": j.auto_bench, "threads": self.lane_threads(), "drives": {}}
        known = self.drives.known_drives()
        for d in known + [x for x in held if x not in known]:
            br = self._drive_bitrate(d)
            out["drives"][d] = {"held": held.get(d, 0),
                                "slots": {s: self.drives.slots(d, s, br) for s in SLOT_STAGES}}
        return out

    def _drive_bitrate(self, drive: str) -> float:
        v = self.db.scalar("SELECT AVG(bitrate) FROM recordings WHERE drive=? AND bitrate > 0", (drive,))
        return float(v) if v else drives_mod.DEFAULT_BITRATE

    def drive_status(self) -> list[dict]:
        """Per drive: benchmark, kind, slots per stage, what runs there now, queued heavy jobs."""
        with self._slots:
            held = [dict(e) for e in self._held.values()]
            bench_busy = set(self._bench_busy)
            wanted = set(self._bench_wanted)
        active = self.drives.status()
        lanes = drives_mod.HEAVY_LANES
        marks = ",".join("?" for _ in lanes)
        queued = {r["drive"]: r["n"] for r in self.db.query(
            f"SELECT r.drive drive, COUNT(*) n FROM jobs j JOIN recordings r ON r.id=j.rec_id"
            f" WHERE j.state='queued' AND j.lane IN ({marks}) GROUP BY r.drive", tuple(lanes))}
        files = {r["drive"]: r for r in self.db.query(
            "SELECT drive, COUNT(*) n, SUM(size) bytes FROM recordings WHERE missing=0 GROUP BY drive")}
        known = self.drives.known_drives()
        for e in held:
            if e["drive"] and e["drive"] not in known:
                known.append(e["drive"])
        out = []
        for d in known:
            bench = self.drives.latest(d)
            br = self._drive_bitrate(d)
            running = [{"job_id": e["job_id"], "rec_id": e["rec_id"], "stage": e["stage"], "lane": e["lane"],
                        "since": e["since"], "kind": "job" if e["job_id"] is not None else "slot"}
                       for e in held if e["drive"] == d]
            state = active.get(d) or ("running" if d in bench_busy else "waiting" if d in wanted else None)
            out.append({
                "drive": d,
                "kind": bench["kind"] if bench else None,
                "bench": bench,
                "bench_state": state,
                "bitrate": br,
                "demand_mb_s": {s: round(drives_mod.demand_mb_s(s, br), 1) for s in SLOT_STAGES},
                "slots": {s: self.drives.slots(d, s, br) for s in SLOT_STAGES},
                "running": running,
                "held": len(running),
                "queued": queued.get(d, 0),
                "files": (files.get(d) or {}).get("n", 0),
                "bytes": (files.get(d) or {}).get("bytes") or 0,
                "roots": [r for r in self.cfg.library.roots if drives_mod.norm_drive(r) == d],
            })
        return out

    # -- workers ------------------------------------------------------------------
    def _spawn(self, lane: str, n: int, stop_evt: threading.Event, tag: str = "") -> list[threading.Thread]:
        out = []
        for i in range(max(1, n)):
            t = threading.Thread(target=self._worker, args=(lane, stop_evt), name=f"job-{lane}-{tag}{i}", daemon=True)
            t.start()
            out.append(t)
        return out

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        lanes = self.lane_threads()
        for lane, n in lanes.items():
            self._threads += self._spawn(lane, n, self._stop)
        j = self.cfg.jobs
        logger.info("[Jobs] workers started lanes=%s parallel=%s gpu_parallel=%d max_parallel=%d",
                    lanes, j.parallel, j.gpu_parallel, j.max_parallel)
        if j.auto_bench and str(j.parallel).lower() == "auto":
            self.drives.start_auto()
        self._size_ocr_pool()

    def _size_ocr_pool(self) -> None:
        """One OCR engine per parallel video job (detect OCRs on the CPU between NVDEC reads)."""
        try:
            from vision.ocr import ocr, ocr_fast
            n = max(1, self.lane_threads()["video"])
            ocr.set_max_engines(n)
            ocr_fast.set_max_engines(n)          # the ticker's fast pool: every parallel detect reads its ticker there
        except Exception:  # noqa: BLE001 - numpy / vision missing is the detect stage's problem
            pass

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        with self._slots:
            self._slots.notify_all()
        self.drives.stop_auto(timeout=min(timeout, 5.0))
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads = []

    def _worker(self, lane: str, stop_evt: threading.Event | None = None) -> None:
        stop_evt = stop_evt or self._stop
        try:
            while not (self._stop.is_set() or stop_evt.is_set()):
                try:
                    if self.paused and not self._ignore_pause:
                        self._wake.wait(1.0)
                        self._wake.clear()
                        continue
                    job = self._claim(lane)
                    if job is None:
                        self._wake.wait(self.poll_s)
                        self._wake.clear()
                        continue
                    self._run(job)
                except Exception:  # noqa: BLE001 - e.g. 'database is locked' in the claim: keep the lane alive
                    logger.error("[Jobs] worker error lane=%s (continuing)", lane, exc_info=True)
                    stop_evt.wait(1.0)
        finally:
            self.db.close_thread()

    def run_pending(self, lane: str | None = None, max_jobs: int | None = None) -> int:
        """Synchronous drain (tests): run queued jobs one at a time on this thread."""
        n = 0
        while max_jobs is None or n < max_jobs:
            job = None
            for ln in ([lane] if lane else LANES):
                job = self._claim(ln)
                if job is not None:
                    break
            if job is None:
                break
            self._run(job)
            n += 1
        return n

    def _lanes_idle(self, lanes: tuple, queued_too: bool = True) -> bool:
        with self._slots:
            busy = any(e["job_id"] is not None and e["lane"] in lanes for e in self._held.values())
        if busy:
            return False
        if not queued_too:
            return True
        marks = ",".join("?" for _ in lanes)
        return not self.db.scalar(f"SELECT COUNT(*) FROM jobs WHERE state='queued' AND lane IN ({marks})", tuple(lanes))

    def drain(self, lanes=None, timeout: float | None = None, ignore_pause: bool = True, poll_s: float = 0.25) -> dict:
        """Work the queued jobs of `lanes` with the threaded workers (drive slots and the GPU cap
        apply, so an SSD and an HDD run side by side) and return when nothing in those lanes is queued or
        running. Lanes without running workers get temporary ones. Ctrl+C once: start nothing new
        and wait for the running jobs; twice: tell them to stop. Returns a summary dict."""
        lanes = tuple(lanes or LANES)
        before = Counter(self.finished)
        stop_evt = threading.Event()
        threads: list[threading.Thread] = []
        have = {t.name.split("-")[1] for t in self._threads if t.is_alive()}
        prev_ignore, self._ignore_pause = self._ignore_pause, bool(ignore_pause)
        old_poll, self.poll_s = self.poll_s, min(self.poll_s, 0.5)
        for lane in lanes:
            if lane not in have:
                threads += self._spawn(lane, self.lane_threads()[lane], stop_evt, tag="d")
        if "video" in lanes:
            self._size_ocr_pool()
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        interrupted = aborted = timed_out = False
        t0 = time.monotonic()
        try:
            while True:
                try:
                    if self._lanes_idle(lanes, queued_too=not interrupted):
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        timed_out = True
                        break
                    self._stop.wait(poll_s)
                    if self._stop.is_set() and not aborted:
                        aborted = True
                        break
                except KeyboardInterrupt:
                    if interrupted:
                        logger.warning("[Jobs] Ctrl+C again: telling the running jobs to stop")
                        aborted = True
                        self._stop.set()
                        break
                    interrupted = True
                    self._no_claim = True
                    logger.warning("[Jobs] Ctrl+C: no new jobs, finishing the running ones (Ctrl+C again aborts them)")
        finally:
            stop_evt.set()
            self._wake.set()
            for t in threads:
                t.join(timeout=30.0 if not aborted else 10.0)
            self._no_claim = False
            self._ignore_pause = prev_ignore
            self.poll_s = old_poll
        ran = Counter(self.finished)
        ran.subtract(before)
        by_stage: dict[str, dict] = {}
        for (stage, state), n in ran.items():
            if n > 0:
                by_stage.setdefault(stage, {})[state] = n
        marks = ",".join("?" for _ in lanes)
        left = self.db.scalar(f"SELECT COUNT(*) FROM jobs WHERE state='queued' AND lane IN ({marks})", tuple(lanes)) or 0
        out = {"ran": sum(n for n in ran.values() if n > 0), "by_stage": by_stage, "queued_left": int(left),
               "interrupted": interrupted, "aborted": aborted, "timed_out": timed_out,
               "seconds": round(time.monotonic() - t0, 1)}
        logger.info("[Jobs] drain lanes=%s ran=%d left=%d %.0fs%s", ",".join(lanes), out["ran"], out["queued_left"],
                    out["seconds"], " (interrupted)" if interrupted else "")
        return out

    def _claim(self, lane: str) -> dict | None:
        """The highest-priority queued job of `lane` whose drive (and the GPU, for video) has a free
        slot; its slot is reserved here and released by _run."""
        if self._no_claim:
            return None
        if lane == "video":
            with self._slots:
                if self._gpu_held() >= max(1, int(self.cfg.jobs.gpu_parallel)):
                    return None
        con = self.db.connect()
        chosen = None
        with self._claim_lock:
            con.execute("BEGIN IMMEDIATE")
            try:
                # a queued job waits while its twin (same recording + stage + arg) runs: enqueue(after_running=True)
                running = {(x[0], x[1], x[2]) for x in con.execute(
                    "SELECT rec_id, stage, arg FROM jobs WHERE state='running'").fetchall()}
                cur = con.execute(
                    "SELECT j.*, r.drive AS _drive, r.bitrate AS _bitrate, r.size AS _size, r.duration_s AS _dur"
                    " FROM jobs j LEFT JOIN recordings r ON r.id=j.rec_id"
                    " WHERE j.state='queued' AND j.lane=? ORDER BY j.priority DESC, j.id ASC", (lane,))
                with self._slots:
                    for row in cur:
                        if running and (row["rec_id"], row["stage"], row["arg"]) in running:
                            continue
                        drive = row["_drive"] or ""
                        br = drives_mod.rec_bitrate({"bitrate": row["_bitrate"], "size": row["_size"],
                                                     "duration_s": row["_dur"]})
                        if self._refusal(drive, row["stage"], lane, br) is None:
                            chosen = row
                            self._reserve(("job", row["id"]), drive, row["stage"], lane, row["id"], row["rec_id"], bitrate=br)
                            break
                if chosen is None:
                    con.execute("COMMIT")
                    return None
                con.execute("UPDATE jobs SET state='running', started_utc=?, progress=0, message='' WHERE id=?",
                            (time.time(), chosen["id"]))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                if chosen is not None:
                    self._release(("job", chosen["id"]))
                raise
        job = {k: chosen[k] for k in chosen.keys() if not k.startswith("_")}
        if chosen["_drive"] and lane in drives_mod.HEAVY_LANES:
            logger.debug("[Jobs] claim lane=%s stage=%s rec=%s drive=%s", lane, job["stage"], job["rec_id"], chosen["_drive"])
        return job

    def _run(self, job: dict) -> None:
        token = ("job", job["id"])
        with self._slots:
            ent = self._held.get(token)
        prev_slot = getattr(self._tls, "slot", None)
        prev_gpu = getattr(self._tls, "gpu", 0)
        self._tls.slot = (ent["drive"], job["stage"]) if ent and ent["drive"] else None
        self._tls.gpu = prev_gpu + (1 if ent and ent["gpu"] else 0)
        self._running[job["id"]] = job
        self._cancel[job["id"]] = threading.Event()
        handler = self.handlers.get(job["stage"])
        t0 = time.monotonic()
        state = "failed"
        try:
            # inside the try: anything raising here must still release the drive / GPU slot
            rec = self.db.one("SELECT * FROM recordings WHERE id=?", (job["rec_id"],)) if job["rec_id"] else None
            set_context((rec or {}).get("name", "")[:10])
            self.bus.publish("job", action="started", job={"id": job["id"], "rec_id": job["rec_id"], "stage": job["stage"],
                                                           "drive": (ent or {}).get("drive")})
            if handler is None:
                raise RuntimeError(f"no handler registered for stage {job['stage']!r}")
            if rec is None and job["rec_id"]:
                raise RuntimeError("recording row vanished")
            ctx = JobContext(self, job, rec)
            result = handler(ctx)
            msg = (result if isinstance(result, str) else "")[:200]
            self.db.execute("UPDATE jobs SET state='done', progress=1, message=?, finished_utc=? WHERE id=?",
                            (msg, time.time(), job["id"]))
            state = "done"
            logger.info("[Jobs] done stage=%s rec=%s %.1fs %s", job["stage"], job["rec_id"], time.monotonic() - t0, msg)
            self.bus.publish("job", action="done", job={"id": job["id"], "rec_id": job["rec_id"], "stage": job["stage"],
                                                        "message": msg})
        except Exception as e:  # noqa: BLE001 - a failing stage must never take the worker down
            ev = self._cancel.get(job["id"])
            if ev is not None and ev.is_set():
                # Stop was pressed: not a failure
                state = "skipped"
                self.db.execute("UPDATE jobs SET state='skipped', message='stopped by you', finished_utc=? WHERE id=?",
                                (time.time(), job["id"]))
                logger.info("[Jobs] stopped stage=%s rec=%s", job["stage"], job["rec_id"])
                self.bus.publish("job", action="skipped", job={"id": job["id"], "rec_id": job["rec_id"], "stage": job["stage"]})
            elif self._stop.is_set():
                # the app is shutting down mid-job: back in the queue, resumed on the next start
                state = "queued"
                self.db.execute("UPDATE jobs SET state='queued', progress=0, message='interrupted by shutdown',"
                                " started_utc=NULL WHERE id=?", (job["id"],))
                logger.info("[Jobs] interrupted by shutdown stage=%s rec=%s -> queued", job["stage"], job["rec_id"])
            else:
                err = f"{type(e).__name__}: {e}"
                logger.error("[Jobs] failed stage=%s rec=%s err=%s", job["stage"], job["rec_id"], err)
                logger.debug("[Jobs] traceback\n%s", traceback.format_exc())
                self.db.execute("UPDATE jobs SET state='failed', error=?, finished_utc=? WHERE id=?",
                                (err[:500], time.time(), job["id"]))
                self.bus.publish("job", action="failed", job={"id": job["id"], "rec_id": job["rec_id"], "stage": job["stage"],
                                                              "error": err[:200]})
        finally:
            self._running.pop(job["id"], None)
            self._cancel.pop(job["id"], None)
            self._tls.slot = prev_slot
            self._tls.gpu = prev_gpu
            self._release(token)
            with self._stats_lock:
                self.finished[(job["stage"], state)] += 1
            set_context("")
