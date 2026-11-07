"""Drive-aware parallelism: how many footage-heavy jobs each library drive can feed at once.

On a typical setup with an NVMe SSD and a 7200 rpm SATA HDD, a detect keyframe pass still reads the
whole 220-240 Mb/s file at ~7-15x realtime (~300-500 MB/s), so the HDD feeds about one at a time while the NVMe feeds several.

``benchmark()`` is a short read-only throughput test on the library's own footage files of
one drive: sequential runs of large blocks at random offsets of the largest files with 1, 2
and 4 concurrent readers on different files. On Windows it bypasses the file cache
(CreateFileW + FILE_FLAG_NO_BUFFERING into a page-aligned buffer); elsewhere, or when that
open fails, it reads normally at offsets this process has not read recently and says so
(``cache_bypass`` False). Nothing is ever written, and nothing but library files is opened.

``slots()`` turns the latest benchmark of a drive into a per-stage job count:
    clamp(min(conc_n, floor(aggregate_mb_s / demand_mb_s(stage, bitrate))), 1, max_parallel)
where ``conc_n`` is how many concurrent readers the drive takes without losing aggregate
throughput (see ``_scaling``) and demand = recording bitrate x the stage's realtime factor.
``DriveManager`` keeps the latest result per drive (table ``drive_bench``), runs a benchmark
once per unbenchmarked drive in the background when it is idle, and on request.
"""

import json
import logging
import math
import mmap
import os
import random
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("wclips.drives")

DEFAULT_BITRATE = 230_000_000          # bits/s: typical 3440x1440 60 fps OBS footage (220-240 Mb/s)
# how many times realtime a stage reads its recording (MB/s demand = bitrate / 8 x factor)
STAGE_FACTOR = {
    "detect": 10.0,         # NVDEC keyframe pass, ~7-15x realtime measured
    "prep": 6.0,
    "proxy": 6.0,           # NVDEC -> NVENC full decode
    "refine": 2.0,          # seeks round candidates
    "transcribe": 30.0,     # the -vn audio extraction demuxes the whole file at disk speed (not measured)
}
DEFAULT_FACTOR = 6.0
HEAVY_LANES = ("video", "stt")         # stages on these lanes read the whole file: they take a drive slot
READERS = (1, 2, 4)
GAIN = 1.15                            # best_n: an extra reader must add >= 15 % aggregate throughput
KEEP = 0.90                            # conc_n: more readers are fine while the aggregate stays >= 90 % of the best
ALIGN = 1 << 20                        # offsets on 1 MiB boundaries (a multiple of every sector size)


class BenchError(RuntimeError):
    pass


def norm_drive(x: str) -> str:
    """'z', 'Z:', 'Z:\\OBS', 'z:/x.mp4' -> 'Z:'; UNC paths -> '\\\\SERVER\\SHARE' (upper case, as
    core.library._drive stores recordings.drive)."""
    x = (x or "").strip()
    if len(x) == 1 and x.isalpha():
        return x.upper() + ":"
    if len(x) >= 2 and x[1] == ":":
        return x[:2].upper()
    if x.startswith("\\\\") or x.startswith("//"):
        d = os.path.splitdrive(x.replace("/", "\\"))[0]
        return (d or x).upper()
    return x


def aggregate_mb_s(bench: dict | None) -> float | None:
    """The best aggregate read rate the benchmark measured on a drive (any reader count)."""
    if not bench or not bench.get("mb_s"):
        return None
    try:
        return max(float(v) for v in bench["mb_s"].values())
    except (TypeError, ValueError):
        return None


def demand_mb_s(stage: str, bitrate: float | None = None) -> float:
    """MB/s one job of this stage pulls from its drive."""
    br = float(bitrate or 0) or DEFAULT_BITRATE
    return br / 8.0 / 1e6 * STAGE_FACTOR.get(stage, DEFAULT_FACTOR)


def rec_bitrate(rec: dict | None) -> float:
    """Bitrate of a recording row: the probed value, else size x 8 / duration, else the default."""
    if not rec:
        return DEFAULT_BITRATE
    if rec.get("bitrate"):
        return float(rec["bitrate"])
    if rec.get("size") and rec.get("duration_s"):
        return float(rec["size"]) * 8.0 / max(1.0, float(rec["duration_s"]))
    return DEFAULT_BITRATE


def _scaling(mb_s: dict[int, float]) -> tuple[int, int]:
    """(best_n, conc_n) from {n: aggregate MB/s}.

    best_n: walk 1 -> 2 -> 4, keep the larger n while its aggregate is >= GAIN x the previous one.
    conc_n: the largest n whose aggregate is still >= KEEP x the best seen so far -- concurrency that
    does not hurt. An NVMe saturates with one 8 MB reader (best_n 1) but loses nothing with four
    (conc_n 4); an HDD loses to seeking (both 1)."""
    ns = sorted(n for n in mb_s if mb_s[n] > 0)
    if not ns:
        return 1, 1
    best_n = ns[0]
    for n in ns[1:]:
        if mb_s[n] >= GAIN * mb_s[best_n]:
            best_n = n
        else:
            break
    conc_n, top = ns[0], mb_s[ns[0]]
    for n in ns[1:]:
        if mb_s[n] >= KEEP * top:
            conc_n = n
            top = max(top, mb_s[n])
        else:
            break
    return best_n, conc_n


def slots(bench: dict | None, stage: str, bitrate: float | None = None, parallel: str = "auto",
          max_parallel: int = 4) -> int:
    """Concurrent jobs of `stage` one drive takes. Fixed mode: that number; auto: from the benchmark
    (unbenchmarked -> 1). When the drive still scaled at the largest reader count tested, the
    bandwidth term alone limits it (the benchmark could not see where it stops)."""
    mx = max(1, int(max_parallel))
    p = str(parallel).strip().lower()
    if p != "auto":
        try:
            return max(1, min(mx, int(float(p))))
        except ValueError:
            return 1
    if not bench or not bench.get("mb_s"):
        return 1
    mb = {int(k): float(v) for k, v in bench["mb_s"].items()}
    conc = int(bench.get("conc_n") or bench.get("best_n") or 1)
    agg = max(mb.values())
    if conc >= max(mb):
        conc = mx                                   # still scaling at the top: bandwidth decides
    by_bw = math.floor(agg / max(0.1, demand_mb_s(stage, bitrate)))
    return max(1, min(mx, conc, by_bw))


# -- readers --------------------------------------------------------------------------------
class _PlainReader:
    """Buffered-off reads through the OS cache (fallback). Offsets avoid what this process
    read in the last hour so the cache is unlikely to serve them."""
    bypass = False

    def __init__(self, path: str, block: int):
        self.path = path
        self.block = block
        self.f = open(path, "rb", buffering=0)        # read-only
        self.size = os.fstat(self.f.fileno()).st_size
        self.buf = bytearray(block)

    def read_at(self, offset: int, n: int) -> int:
        self.f.seek(offset)
        mv = memoryview(self.buf)[:n]
        got = self.f.readinto(mv)
        return got or 0

    def close(self) -> None:
        try:
            self.f.close()
        except OSError:
            pass


class _UnbufferedReader:
    """Windows: CreateFileW(GENERIC_READ, FILE_FLAG_NO_BUFFERING | FILE_FLAG_SEQUENTIAL_SCAN) and
    ReadFile into a page-aligned anonymous mmap: the bytes come from the disk, not the cache."""
    bypass = True

    def __init__(self, path: str, block: int):
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k32.ReadFile.restype = wintypes.BOOL
        k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong),
                                         wintypes.DWORD]
        k32.SetFilePointerEx.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        self._ct, self._k32, self._wt = ctypes, k32, wintypes
        GENERIC_READ = 0x80000000
        SHARE = 0x1 | 0x2 | 0x4                       # read | write | delete: never get in OBS's way
        OPEN_EXISTING = 3
        FLAGS = 0x20000000 | 0x08000000               # NO_BUFFERING | SEQUENTIAL_SCAN
        self.path = path
        self.block = block = max(4096, -(-int(block) // 4096) * 4096)     # whole sectors
        self.size = os.path.getsize(path)
        h = k32.CreateFileW(str(path), GENERIC_READ, SHARE, None, OPEN_EXISTING, FLAGS, None)
        if h is None or h == wintypes.HANDLE(-1).value:
            raise OSError(ctypes.get_last_error(), f"CreateFileW no-buffering failed: {path}")
        self.h = h
        self.mm = mmap.mmap(-1, block)                # VirtualAlloc: page (64 KiB) aligned
        self.cbuf = (ctypes.c_char * block).from_buffer(self.mm)
        self.addr = ctypes.addressof(self.cbuf)

    def read_at(self, offset: int, n: int) -> int:
        ct = self._ct
        offset -= offset % 4096
        n = min(self.block, max(4096, -(-int(n) // 4096) * 4096))
        if not self._k32.SetFilePointerEx(self.h, offset, None, 0):
            raise OSError(ct.get_last_error(), "SetFilePointerEx failed")
        got = self._wt.DWORD(0)
        if not self._k32.ReadFile(self.h, self.addr, n, ct.byref(got), None):
            raise OSError(ct.get_last_error(), "ReadFile failed")
        return int(got.value)

    def close(self) -> None:
        try:
            self._k32.CloseHandle(self.h)
        finally:
            del self.cbuf                             # release the buffer export before closing the map
            self.mm.close()


_recent_lock = threading.Lock()
_recent: dict[str, dict[int, float]] = {}            # path -> {aligned offset: utc read}
_RECENT_S = 3600.0


def _pick_offset(rng: random.Random, path: str, size: int, span: int, avoid: bool) -> int:
    hi = max(0, size - span)
    if hi < ALIGN:
        return 0
    for _ in range(20):
        off = rng.randrange(0, hi // ALIGN + 1) * ALIGN
        if not avoid:
            return off
        with _recent_lock:
            seen = _recent.setdefault(path, {})
            now = time.time()
            if all(abs(off - o) >= span or now - t > _RECENT_S for o, t in seen.items()):
                seen[off] = now
                return off
    return off


def _open_reader(path: str, block: int, force_plain: bool = False):
    if not force_plain and sys.platform == "win32":
        try:
            return _UnbufferedReader(path, block)
        except Exception as e:  # noqa: BLE001 - fall back to cached reads, the result says so
            logger.info("[Drives] no-buffering open failed, plain reads instead path=%s err=%s", path, e)
    return _PlainReader(path, block)


def _drive_type_is_remote(path: str) -> bool:
    if path.startswith("\\\\") or path.startswith("//"):
        return True
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        root = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
        return ctypes.windll.kernel32.GetDriveTypeW(root) == 4           # DRIVE_REMOTE
    except Exception:  # noqa: BLE001
        return False


def guess_kind(mb_s: dict[int, float], conc_n: int, remote: bool = False) -> str:
    if remote:
        return "network"
    m1 = mb_s.get(1, 0.0)
    top = max(mb_s.values()) if mb_s else 0.0
    if m1 >= 450 or top >= 600 or (conc_n >= 2 and top >= 300):
        return "ssd"
    return "hdd"


def library_files(root_or_dir: str, patterns=("*.mp4", "*.mkv", "*.mov"), limit: int = 8) -> list[str]:
    """The largest footage files under a library directory (used when no db is at hand)."""
    p = Path(root_or_dir)
    if not p.is_dir():
        return []
    import fnmatch
    out = []
    for dirpath, _dirs, names in os.walk(p):
        for n in names:
            if any(fnmatch.fnmatch(n.lower(), pat) for pat in patterns):
                fp = os.path.join(dirpath, n)
                try:
                    out.append((os.path.getsize(fp), fp))
                except OSError:
                    pass
    out.sort(reverse=True)
    return [fp for _s, fp in out[:limit]]


def benchmark(root_or_drive: str, files: list[str] | None = None, *, block: int = 8 << 20, run_blocks: int = 8,
              readers=READERS, cap_bytes: int = 1_500_000_000, max_s: float = 20.0, force_plain: bool = False,
              opener=None, seed: int | None = None) -> dict:
    """Read-only throughput of one drive with 1 / 2 / 4 concurrent readers on different files.

    files: the footage files to read (the largest first); default = the largest under the
    directory `root_or_drive`. Total read <= cap_bytes and wall time ~<= max_s, split evenly over
    the reader counts. opener(path, block) -> reader is for tests."""
    if files is None:
        files = library_files(root_or_drive)
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        raise BenchError(f"no footage files to read on {root_or_drive}")
    drive = norm_drive(root_or_drive)
    rng = random.Random(seed)
    readers = tuple(sorted(set(int(n) for n in readers if int(n) >= 1))) or READERS
    phase_bytes = max(block, cap_bytes // len(readers))
    phase_s = max(0.2, max_s / len(readers))
    span = block * max(1, run_blocks)
    open_fn = opener or (lambda p, b: _open_reader(p, b, force_plain))
    mb_s: dict[int, float] = {}
    bypass_all = True
    total_bytes = 0
    t_all = time.monotonic()
    notes: list[str] = []
    for n in readers:
        rd = []
        try:
            for i in range(n):
                rd.append(open_fn(files[i % len(files)], block))
        except Exception:
            for r in rd:
                r.close()
            raise
        if n > len(files):
            notes.append(f"n{n} shares {len(files)} files")
        bypass_all = bypass_all and all(getattr(r, "bypass", False) for r in rd)
        lock = threading.Lock()
        state = {"bytes": 0, "err": None}
        start = threading.Barrier(n + 1)
        deadline = [0.0]

        def work(r, i):
            local = random.Random(rng.random() + i)
            try:
                start.wait()
                while True:
                    off = _pick_offset(local, r.path, r.size, span, avoid=not getattr(r, "bypass", False))
                    for k in range(max(1, run_blocks)):
                        pos = off + k * block
                        if pos >= r.size and k:
                            break
                        got = r.read_at(pos if pos < r.size else 0, block)
                        with lock:
                            state["bytes"] += got
                            done = state["bytes"] >= phase_bytes
                        if done or time.monotonic() >= deadline[0] or got <= 0:
                            return
            except Exception as e:  # noqa: BLE001
                with lock:
                    state["err"] = state["err"] or e

        threads = [threading.Thread(target=work, args=(r, i), name=f"bench-{drive}-{n}-{i}", daemon=True)
                   for i, r in enumerate(rd)]
        for t in threads:
            t.start()
        deadline[0] = time.monotonic() + phase_s
        t0 = time.monotonic()
        start.wait()
        for t in threads:
            t.join()
        dt = max(1e-6, time.monotonic() - t0)
        for r in rd:
            r.close()
        if state["err"] is not None:
            raise BenchError(f"read failed with {n} readers: {state['err']}")
        mb_s[n] = round(state["bytes"] / dt / 1e6, 1)
        total_bytes += state["bytes"]
    best_n, conc_n = _scaling(mb_s)
    kind = guess_kind(mb_s, conc_n, _drive_type_is_remote(files[0]))
    if not bypass_all:
        notes.append("file cache not bypassed: plain reads at offsets not read recently")
    res = {"drive": drive, "mb_s": {str(k): v for k, v in mb_s.items()}, "best_n": best_n, "conc_n": conc_n,
           "kind": kind, "cache_bypass": bool(bypass_all), "files": len(files), "bytes": int(total_bytes),
           "seconds": round(time.monotonic() - t_all, 2), "block": block, "note": "; ".join(notes), "utc": time.time()}
    logger.info("[Drives] bench drive=%s %s best_n=%d conc_n=%d kind=%s bypass=%s read=%.0fMB in %.1fs",
                drive, " ".join(f"n{k}={v:.0f}MB/s" for k, v in mb_s.items()), best_n, conc_n, kind, bypass_all,
                total_bytes / 1e6, res["seconds"])
    return res


# -- persistence + background runner ----------------------------------------------------------
def _row_to_bench(row: dict) -> dict:
    out = dict(row)
    try:
        out["mb_s"] = json.loads(out.get("mb_s") or "{}")
    except json.JSONDecodeError:
        out["mb_s"] = {}
    out["cache_bypass"] = bool(out.get("cache_bypass"))
    return out


class DriveManager:
    """Latest benchmark per drive (cached), on-request and automatic benchmarks. Exclusivity
    against footage-heavy jobs goes through the JobQueue (bench_begin / bench_end)."""

    AUTO_POLL_S = 30.0
    REQUEST_WAIT_S = 900.0             # an on-request benchmark waits this long for the drive to go idle
    # auto: a drive with a backlog is never idle at a poll (the next job is claimed at once), so an
    # unbenchmarked drive busy this long is benchmarked anyway: new heavy claims there hold, the
    # running job finishes, then the benchmark runs (still never while a footage job runs there)
    AUTO_PATIENCE_S = 120.0
    AUTO_WAIT_S = 900.0

    def __init__(self, cfg, db, bus, queue=None):
        self.cfg = cfg
        self.db = db
        self.bus = bus
        self.queue = queue
        self._lock = threading.Lock()
        self._cache: dict[str, dict] | None = None
        self._active: dict[str, str] = {}           # drive -> 'waiting' | 'running'
        self._tried: set[str] = set()               # auto: once per drive per process
        self._busy_since: dict[str, float] = {}     # auto: drive -> monotonic time first seen busy
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.bench_fn = benchmark                    # tests swap this

    # -- results ------------------------------------------------------------------------
    def _load(self) -> dict[str, dict]:
        with self._lock:
            if self._cache is None:
                cache = {}
                for row in self.db.query("SELECT * FROM drive_bench ORDER BY utc ASC, id ASC"):
                    cache[row["drive"]] = _row_to_bench(row)
                self._cache = cache
            return self._cache

    def latest(self, drive: str) -> dict | None:
        return self._load().get(norm_drive(drive))

    def latest_all(self) -> dict[str, dict]:
        return dict(self._load())

    def store(self, res: dict) -> dict:
        rid = self.db.execute(
            "INSERT INTO drive_bench (drive, kind, mb_s, best_n, conc_n, cache_bypass, files, bytes, seconds, note, utc)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (res["drive"], res["kind"], json.dumps(res["mb_s"]), int(res["best_n"]), int(res.get("conc_n") or res["best_n"]),
             int(bool(res["cache_bypass"])), int(res.get("files") or 0), int(res.get("bytes") or 0),
             float(res.get("seconds") or 0), res.get("note") or "", float(res.get("utc") or time.time())))
        row = self.db.one("SELECT * FROM drive_bench WHERE id=?", (rid,))
        b = _row_to_bench(row)
        self._load()
        with self._lock:
            self._cache[b["drive"]] = b
        return b

    def slots(self, drive: str, stage: str, bitrate: float | None = None) -> int:
        j = self.cfg.jobs
        return slots(self.latest(drive), stage, bitrate, j.parallel, j.max_parallel)

    # -- which files / drives --------------------------------------------------------------
    def known_drives(self) -> list[str]:
        rows = self.db.query("SELECT DISTINCT drive FROM recordings WHERE missing=0 AND drive != '' ORDER BY drive")
        out = [r["drive"] for r in rows]
        for root in self.cfg.library.roots:
            d = norm_drive(root)
            if d and d not in out and Path(root).is_dir():
                out.append(d)
        return out

    def files_for(self, drive: str, limit: int = 8) -> list[str]:
        """The largest library files of this drive (the only files a benchmark opens)."""
        rows = self.db.query("SELECT path FROM recordings WHERE drive=? AND missing=0 ORDER BY size DESC LIMIT ?",
                             (norm_drive(drive), limit * 3))
        out = [r["path"] for r in rows if os.path.isfile(r["path"])]
        return out[:limit]

    def status(self) -> dict[str, str]:
        with self._lock:
            return dict(self._active)

    # -- running a benchmark ---------------------------------------------------------------
    def run(self, drive: str, wait_s: float = 0.0) -> dict | None:
        """Benchmark one drive now (blocking). The queue stops handing out heavy jobs on the drive
        while this waits up to wait_s for it to go idle, then for the benchmark itself. Returns the
        stored result, or None when the drive never went idle / is already being benchmarked."""
        drive = norm_drive(drive)
        with self._lock:
            if drive in self._active:
                return None
            self._active[drive] = "waiting"
        self.bus.publish("drives", action="bench_waiting", drive=drive)
        began = False
        try:
            files = self.files_for(drive)
            if not files:
                raise BenchError(f"no library files on {drive}")
            if self.queue is not None:
                began = self.queue.bench_begin(drive, wait_s)
                if not began:
                    logger.info("[Drives] bench skipped drive=%s: footage jobs still running", drive)
                    self.bus.publish("drives", action="bench_skipped", drive=drive, reason="busy")
                    return None
            with self._lock:
                self._active[drive] = "running"
            self.bus.publish("drives", action="bench_started", drive=drive)
            res = self.bench_fn(drive, files)
            b = self.store(res)
            self.bus.publish("drives", action="bench_done", drive=drive, bench=b)
            return b
        except Exception as e:  # noqa: BLE001 - a failed benchmark leaves the drive at 1 slot
            logger.warning("[Drives] bench failed drive=%s err=%s", drive, e)
            self.bus.publish("drives", action="bench_failed", drive=drive, error=str(e)[:200])
            return None
        finally:
            if began and self.queue is not None:
                self.queue.bench_end(drive)
            with self._lock:
                self._active.pop(drive, None)

    def request(self, drive: str | None = None) -> list[str]:
        """Re-run on request, in a background thread per drive. Returns the drives started."""
        drives = [norm_drive(drive)] if drive else self.known_drives()
        started = []
        for d in drives:
            if d in self.status():
                continue
            threading.Thread(target=self._run_thread, args=(d, self.REQUEST_WAIT_S), name=f"bench-{d}", daemon=True).start()
            started.append(d)
        return started

    def _run_thread(self, drive: str, wait_s: float) -> None:
        try:
            self.run(drive, wait_s)
        finally:
            self.db.close_thread()

    def missing(self) -> list[str]:
        have = self.latest_all()
        return [d for d in self.known_drives() if d not in have]

    def bench_missing(self) -> list[dict]:
        """Synchronously benchmark every drive without a result (headless scan, drives idle)."""
        out = []
        for d in self.missing():
            b = self.run(d, wait_s=0.0)
            if b:
                out.append(b)
        return out

    # -- automatic --------------------------------------------------------------------------
    def start_auto(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._auto_loop, name="drive-bench", daemon=True)
        self._thread.start()

    def stop_auto(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def auto_once(self) -> list[str]:
        """One pass of the automatic benchmark: each unbenchmarked drive, once per process, only
        when no footage-heavy job is running on it. A drive busy for AUTO_PATIENCE_S (a backlog)
        is benchmarked with a wait: no new heavy job starts there until the running ones end."""
        j = self.cfg.jobs
        if not j.auto_bench or str(j.parallel).lower() != "auto":
            return []
        done = []
        for d in self.missing():
            if d in self._tried:
                continue
            wait_s = 0.0
            if self.queue is not None and self.queue.drive_busy(d):
                since = self._busy_since.setdefault(d, time.monotonic())
                if time.monotonic() - since < self.AUTO_PATIENCE_S:
                    continue
                wait_s = self.AUTO_WAIT_S
                logger.info("[Drives] auto bench drive=%s busy for %.0fs: holding new jobs there until it is idle",
                            d, time.monotonic() - since)
            self._busy_since.pop(d, None)
            self._tried.add(d)
            if self.run(d, wait_s=wait_s):
                done.append(d)
        return done

    def _auto_loop(self) -> None:
        self._stop.wait(5.0)                         # let startup settle
        while not self._stop.is_set():
            try:
                self.auto_once()
            except Exception:  # noqa: BLE001
                logger.warning("[Drives] auto bench pass failed", exc_info=True)
            self._stop.wait(self.AUTO_POLL_S)
        self.db.close_thread()
