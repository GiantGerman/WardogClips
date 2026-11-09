"""How busy the graphics card(s) are with 3D work (games), from Windows' own performance counters.

The counter set is "GPU Engine", counter "Utilization Percentage", instances filtered to the 3D engines
(``*engtype_3D``). Windows keeps one instance per process and engine, so the instance NAMES carry process ids.
This module never stores, returns or logs an instance name: each value is added to a machine-wide sum and to a
per-adapter sum keyed ONLY by the adapter's LUID (the ``luid_0x..._0x...`` part of the name); the rest of the name,
the process id included, is dropped on the spot. No process is listed, opened or touched (ban safety).

Vendor-neutral: works for NVIDIA, AMD and Intel cards alike (WDDM 2.x, Windows 10 1709+).

Primary path: PDH through ctypes (PdhOpenQueryW / PdhAddEnglishCounterW / PdhCollectQueryData /
PdhGetFormattedCounterArrayW) with a query kept open between samples (a rate counter needs two collections).
Fallback: ``typeperf`` (two one-second samples). Both None -> the caller decides without this signal.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import subprocess
import threading
import time

logger = logging.getLogger("wclips.gpuload")

COUNTER = r"\GPU Engine(*engtype_3D)\Utilization Percentage"
REOPEN_S = 600.0            # rebuild the PDH query now and then (new adapters, a driver reset)
FIRST_GAP_S = 0.5           # gap between the first two collections of a fresh query
RETRY_S = 300.0             # a reader that failed is tried again after this long (a driver reset is not forever)
TYPEPERF_EVERY_S = 15.0     # typeperf is a child process of ~2 s: its answer is reused this long
_NO_WINDOW = 0x08000000
_LUID_RE = re.compile(r"luid_(0x[0-9a-f]+_0x[0-9a-f]+)", re.I)

PDH_FMT_DOUBLE = 0x00000200
PDH_FMT_NOCAP100 = 0x00008000
PDH_MORE_DATA = 0x800007D2
PDH_CSTATUS_VALID_DATA = 0x0
PDH_CSTATUS_NEW_DATA = 0x1


def luid_of(instance: str) -> str:
    """The adapter LUID part of a GPU Engine instance name ('0x00000000_0x0000d1b5'), '' if none. Only this part of
    a name ever leaves this module."""
    m = _LUID_RE.search(instance or "")
    return m.group(1).lower() if m else ""


def summarize(pairs) -> dict:
    """[(instance name, value %)] -> {total, peak, adapters: {luid: %}}. Names are reduced to their LUID here and
    then forgotten. `peak` = the busiest adapter, capped at 100 (several processes on one 3D engine add up)."""
    total = 0.0
    adapters: dict[str, float] = {}
    for name, val in pairs:
        try:
            v = float(val)
        except (TypeError, ValueError):
            continue
        if v != v or v < 0:                       # NaN / garbage
            continue
        total += v
        key = luid_of(name) or "unknown"
        adapters[key] = adapters.get(key, 0.0) + v
    adapters = {k: round(min(100.0, v), 1) for k, v in adapters.items()}
    return {"total": round(total, 1), "peak": max(adapters.values()) if adapters else 0.0, "adapters": adapters}


class _Pdh:
    """One open PDH query with the wildcard 3D counter (Windows only)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct = ctypes
        self.pdh = ctypes.WinDLL("pdh.dll")
        self.pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
        self.pdh.PdhOpenQueryW.restype = ctypes.c_long
        self.pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_size_t,
                                                   ctypes.POINTER(ctypes.c_void_p)]
        self.pdh.PdhAddEnglishCounterW.restype = ctypes.c_long
        self.pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
        self.pdh.PdhCollectQueryData.restype = ctypes.c_long
        self.pdh.PdhGetFormattedCounterArrayW.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                          ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                                                          ctypes.c_void_p]
        self.pdh.PdhGetFormattedCounterArrayW.restype = ctypes.c_long
        self.pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
        self.pdh.PdhCloseQuery.restype = ctypes.c_long

        class _Val(ctypes.Structure):           # PDH_FMT_COUNTERVALUE: CStatus + an 8-byte union (double here)
            _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]

        class _Item(ctypes.Structure):          # PDH_FMT_COUNTERVALUE_ITEM_W
            _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _Val)]
        self._Item = _Item
        self.query = ctypes.c_void_p()
        self.counter = ctypes.c_void_p()
        self.opened = 0.0

    def open(self) -> None:
        ct = self.ct
        rc = self.pdh.PdhOpenQueryW(None, 0, ct.byref(self.query))
        if rc != 0:
            raise OSError(f"PdhOpenQueryW 0x{rc & 0xFFFFFFFF:08X}")
        rc = self.pdh.PdhAddEnglishCounterW(self.query, COUNTER, 0, ct.byref(self.counter))
        if rc != 0:
            self.close()
            raise OSError(f"PdhAddEnglishCounterW 0x{rc & 0xFFFFFFFF:08X}")
        self.pdh.PdhCollectQueryData(self.query)
        self.opened = time.monotonic()
        time.sleep(FIRST_GAP_S)

    def close(self) -> None:
        if self.query:
            try:
                self.pdh.PdhCloseQuery(self.query)
            except OSError:
                pass
        self.query = self.ct.c_void_p()
        self.counter = self.ct.c_void_p()

    def read(self) -> list[tuple[str, float]]:
        from ctypes import wintypes
        ct = self.ct
        if not self.query or time.monotonic() - self.opened > REOPEN_S:
            self.close()
            self.open()
        rc = self.pdh.PdhCollectQueryData(self.query)
        if rc != 0:
            self.close()
            raise OSError(f"PdhCollectQueryData 0x{rc & 0xFFFFFFFF:08X}")
        fmt = PDH_FMT_DOUBLE | PDH_FMT_NOCAP100
        for _ in range(3):                       # the instance list can grow between the size call and the read
            size, count = wintypes.DWORD(0), wintypes.DWORD(0)
            rc = self.pdh.PdhGetFormattedCounterArrayW(self.counter, fmt, ct.byref(size), ct.byref(count), None)
            if (rc & 0xFFFFFFFF) != PDH_MORE_DATA:
                if rc == 0:
                    return []                    # no 3D instance at all
                raise OSError(f"PdhGetFormattedCounterArrayW 0x{rc & 0xFFFFFFFF:08X}")
            buf = (ct.c_byte * max(size.value, 1))()
            rc = self.pdh.PdhGetFormattedCounterArrayW(self.counter, fmt, ct.byref(size), ct.byref(count), buf)
            if (rc & 0xFFFFFFFF) == PDH_MORE_DATA:
                continue
            if rc != 0:
                raise OSError(f"PdhGetFormattedCounterArrayW 0x{rc & 0xFFFFFFFF:08X}")
            items = ct.cast(buf, ct.POINTER(self._Item))
            out = []
            for i in range(count.value):
                it = items[i]
                if it.FmtValue.CStatus in (PDH_CSTATUS_VALID_DATA, PDH_CSTATUS_NEW_DATA):
                    # the name goes straight into luid_of(): the rest of it (pid) is never kept
                    out.append((luid_of(it.szName or ""), float(it.FmtValue.doubleValue)))
            return [("luid_" + k if k else "", v) for k, v in out]
        raise OSError("GPU Engine instance list kept changing")


def _typeperf() -> list[tuple[str, float]] | None:
    """Fallback: typeperf's CSV (header = counter paths, last row = values). Header names are reduced to LUIDs."""
    if os.name != "nt":
        return None
    try:
        cp = subprocess.run(["typeperf", COUNTER, "-sc", "2", "-si", "1"], capture_output=True, text=True,
                            timeout=15, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    rows = [r for r in csv.reader(io.StringIO(cp.stdout or "")) if len(r) > 1]
    if len(rows) < 2:
        return None
    rows = [r for r in rows if len(r) == len(rows[0])]       # drops the trailing status line
    if len(rows) < 2:
        return None
    head, last = rows[0], rows[-1]
    luids = ["luid_" + luid_of(h) if luid_of(h) else "" for h in head[1:]]
    out = []
    for k, v in zip(luids, last[1:]):
        try:
            out.append((k, float(v)))
        except ValueError:
            continue
    return out


class GpuLoad:
    """Thread-safe sampler. sample() -> {total, peak, adapters, source} or None when Windows could not say."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pdh: _Pdh | None = None
        self._pdh_broken = os.name != "nt"
        self._typeperf_broken = os.name != "nt"
        self.source = ""
        self.last_error = ""
        self._retry_at = 0.0                     # when broken readers get another try
        self._tp_cache: tuple[float, list] | None = None
        self._clock = time.monotonic

    def sample(self) -> dict | None:
        with self._lock:
            pairs = None
            now = self._clock()
            if (self._pdh_broken or self._typeperf_broken) and os.name == "nt" and self._retry_at and now >= self._retry_at:
                self._pdh_broken = self._typeperf_broken = False     # try the readers again
                self._retry_at = 0.0
            if not self._pdh_broken:
                try:
                    if self._pdh is None:
                        self._pdh = _Pdh()
                    pairs = self._pdh.read()
                    self.source = "pdh"
                except Exception as e:  # noqa: BLE001 - any PDH trouble -> typeperf
                    self.last_error = f"pdh: {type(e).__name__}: {e}"[:200]
                    logger.info("[GPU] performance counters not readable through PDH (%s) -- trying typeperf",
                                self.last_error)
                    self._pdh_broken = True
                    self._retry_at = now + RETRY_S
                    if self._pdh is not None:
                        self._pdh.close()
                    self._pdh = None
            if pairs is None and not self._typeperf_broken:
                if self._tp_cache is not None and now - self._tp_cache[0] < TYPEPERF_EVERY_S:
                    pairs = self._tp_cache[1]
                else:
                    pairs = _typeperf()
                    self._tp_cache = None if pairs is None else (now, pairs)
                if pairs is None:
                    self._typeperf_broken = True
                    self._retry_at = now + RETRY_S
                    logger.info("[GPU] typeperf gave no GPU Engine values -- GPU load unknown")
                else:
                    self.source = "typeperf"
            if pairs is None:
                self.source = ""
                return None
            out = summarize(pairs)
            out["source"] = self.source
            return out

    def close(self) -> None:
        with self._lock:
            if self._pdh is not None:
                self._pdh.close()
                self._pdh = None


sampler = GpuLoad()


def sample() -> dict | None:
    """Machine-wide 3D load now: {total, peak, adapters {luid: %}, source} or None."""
    return sampler.sample()


def utilization() -> int | None:
    """The busiest adapter's 3D load in % (0-100), None when unknown."""
    s = sample()
    return None if s is None else int(round(s["peak"]))
