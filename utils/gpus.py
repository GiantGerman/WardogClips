"""The graphics cards in this PC and which of them WardogClips may use.

Hardware facts only (ban safety: no process is ever listed, opened or read):
- DXGI (`IDXGIFactory1::EnumAdapters1` through ctypes): name, PCI vendor / device ids, dedicated VRAM, LUID and the
  software-adapter flag, in the order Direct3D numbers the adapters. That number is what ffmpeg's
  `-hwaccel d3d11va -hwaccel_device <n>` and onnxruntime-DirectML's `device_id` mean.
- Fallback when DXGI cannot be asked: `Win32_VideoController` through PowerShell CIM (names and PCI ids, no LUID).
- With two or more NVIDIA cards the CUDA device number of each comes from the NVIDIA driver (`cuDeviceGetLuid`,
  matched on the LUID); CUDA's order can differ from Direct3D's.
- `WARDOGCLIPS_FAKE_GPUS` = a JSON list replaces the real list (to exercise the AMD / Intel / no-GPU paths on any PC):
  `[{"name": "Radeon RX 7800 XT", "vendor": "amd", "vram_mb": 16384}, ...]` (vendor nvidia | amd | intel | a PCI id;
  optional software, device_id, subsys_id). `[]` = no graphics card.

A card is remembered by a stable key built from its PCI ids (vendor-device-subsystem + a counter for identical cards):
the LUID changes on every boot, the index when a card is added. Settings > Hardware keeps the keys of the cards the
user wants left alone (`hardware.leave_alone`); every other card is used.

What each card can do (`capabilities`):
- decode: `cuda` (NVDEC) on NVIDIA when this ffmpeg has it, else `d3d11va` (any Direct3D 11 card), else none (CPU).
- encode: h264_nvenc / h264_amf / h264_qsv when the card's vendor matches and this ffmpeg lists that encoder.
- speech: whisper (CTranslate2) runs on CUDA only -> NVIDIA only; everything else transcribes on the CPU (int8).
- ocr: onnxruntime-DirectML runs on any hardware Direct3D 12 card (device_id = the Direct3D index); built-in graphics
  are left out (too slow), a job on one reads its HUD text on the best usable dedicated card.

A job's card: the scheduler (core/jobs.py) gives each video job a card from its per-card slot pool and binds it to the
job's thread (`bound(key)`); the ffmpeg readers / encoders, whisper and OCR ask `current()`.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import logging
import os
import subprocess
import threading
import time

logger = logging.getLogger("wclips.gpus")

FAKE_ENV = "WARDOGCLIPS_FAKE_GPUS"
VENDORS = {0x10DE: "nvidia", 0x1002: "amd", 0x1022: "amd", 0x8086: "intel", 0x1414: "microsoft"}
VENDOR_IDS = {"nvidia": 0x10DE, "amd": 0x1002, "intel": 0x8086, "microsoft": 0x1414}
VENDOR_NAMES = {"nvidia": "NVIDIA", "amd": "AMD", "intel": "Intel", "microsoft": "Microsoft", "other": "Other"}
ENCODERS = {"nvidia": "h264_nvenc", "amd": "h264_amf", "intel": "h264_qsv"}
# built-in graphics (iGPU): AMD / Intel with less "dedicated" memory than this (a Ryzen iGPU reports its 2 GB BIOS
# carve-out as dedicated) or with a generic name -- good for decoding, too weak for OCR / speech
INTEGRATED_MB = 4096
_IGPU_NAMES = ("amd radeon(tm) graphics", "amd radeon graphics", "intel(r) uhd graphics", "intel(r) iris(r) xe graphics")
CPU = "cpu"                   # the pool key of video jobs when no card may be used
_NO_WINDOW = 0x08000000
_DXGI_ADAPTER_FLAG_SOFTWARE = 2
_DXGI_ERROR_NOT_FOUND = 0x887A0002

_lock = threading.Lock()
_cache: dict = {"list": None, "source": "", "t": 0.0}
_tls = threading.local()
_cfg_ref: list = []           # [settings] once configure() ran (the live object: Settings POSTs replace sections in it)


# -- enumeration -------------------------------------------------------------------------------------------------
class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]


class _DESC1(ctypes.Structure):
    _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", ctypes.c_uint), ("DeviceId", ctypes.c_uint),
                ("SubSysId", ctypes.c_uint), ("Revision", ctypes.c_uint), ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t), ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuid", _LUID), ("Flags", ctypes.c_uint)]


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]


_IID_IDXGIFactory1 = _GUID(0x770AAE78, 0xF26F, 0x4DBA, (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))


def _vcall(obj: ctypes.c_void_p, index: int, *args, argtypes=()):
    """Call COM method number `index` of `obj` (HRESULT as a plain long: no ctypes exception on failure)."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])
    return fn(obj, *args) & 0xFFFFFFFF


def _release(obj) -> None:
    if obj:
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(obj)


def _dxgi() -> list[dict]:
    """Every adapter DXGI lists, in its order (IDXGIFactory1 vtable: EnumAdapters1 = 12; IDXGIAdapter1: GetDesc1 = 10)."""
    dxgi = ctypes.WinDLL("dxgi.dll")
    create = dxgi.CreateDXGIFactory1
    create.restype = ctypes.c_long
    create.argtypes = [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    factory = ctypes.c_void_p()
    hr = create(ctypes.byref(_IID_IDXGIFactory1), ctypes.byref(factory)) & 0xFFFFFFFF
    if hr != 0 or not factory:
        raise OSError(f"CreateDXGIFactory1 hr=0x{hr:08X}")
    out = []
    try:
        for i in range(32):
            adapter = ctypes.c_void_p()
            hr = _vcall(factory, 12, ctypes.c_uint(i), ctypes.byref(adapter),
                        argtypes=(ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)))
            if hr == _DXGI_ERROR_NOT_FOUND:
                break
            if hr != 0 or not adapter:
                raise OSError(f"EnumAdapters1({i}) hr=0x{hr:08X}")
            try:
                d = _DESC1()
                hr = _vcall(adapter, 10, ctypes.byref(d), argtypes=(ctypes.POINTER(_DESC1),))
                if hr != 0:
                    raise OSError(f"GetDesc1({i}) hr=0x{hr:08X}")
                out.append({"index": i, "name": d.Description.strip(), "vendor_id": int(d.VendorId),
                            "device_id": int(d.DeviceId), "subsys_id": int(d.SubSysId),
                            "vram_mb": int(d.DedicatedVideoMemory // (1024 * 1024)),
                            "shared_mb": int(d.SharedSystemMemory // (1024 * 1024)),
                            "luid": f"{d.AdapterLuid.HighPart & 0xFFFFFFFF:08x}:{d.AdapterLuid.LowPart:08x}",
                            "software": bool(d.Flags & _DXGI_ADAPTER_FLAG_SOFTWARE)})
            finally:
                _release(adapter)
    finally:
        _release(factory)
    return out


def _wmi() -> list[dict]:
    """Win32_VideoController through PowerShell CIM (no LUID; the order is WMI's, best effort for the index)."""
    ps = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                      "powershell.exe")
    cmd = [ps if os.path.isfile(ps) else "powershell", "-NoProfile", "-NonInteractive", "-Command",
           "Get-CimInstance Win32_VideoController | Select-Object Name,PNPDeviceID,AdapterRAM | ConvertTo-Json -Compress"]
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=20, creationflags=_NO_WINDOW)
    data = json.loads(cp.stdout or "[]")
    if isinstance(data, dict):
        data = [data]
    out = []
    for i, row in enumerate(data or []):
        pnp = str(row.get("PNPDeviceID") or "").upper()

        def _hex(tag: str) -> int:
            k = pnp.find(tag)
            try:
                return int(pnp[k + len(tag):k + len(tag) + (8 if tag == "SUBSYS_" else 4)], 16) if k >= 0 else 0
            except ValueError:
                return 0
        ven = _hex("VEN_")
        ram = int(row.get("AdapterRAM") or 0)
        out.append({"index": i, "name": str(row.get("Name") or "").strip(), "vendor_id": ven,
                    "device_id": _hex("DEV_"), "subsys_id": _hex("SUBSYS_"),
                    "vram_mb": ram // (1024 * 1024), "shared_mb": 0, "luid": "",
                    # AdapterRAM is a 32-bit number: a card with 4 GB or more reads as about 4095 MB
                    "vram_capped": ram >= 0xFFF00000,
                    "software": ven == 0x1414 or not pnp.startswith("PCI\\")})
    # Direct3D numbers the hardware cards first and the software adapters after them; WMI lists remote / virtual
    # display adapters anywhere, so number the hardware cards 0.. in WMI's order (still a guess with 2+ cards)
    hw_rows = [a for a in out if not a["software"]] + [a for a in out if a["software"]]
    for i, a in enumerate(hw_rows):
        a["index"] = i
    return hw_rows


def _fake(raw: str) -> list[dict]:
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError(f"{FAKE_ENV} must be a JSON list")
    out = []
    for i, row in enumerate(data):
        row = row if isinstance(row, dict) else {"name": str(row)}
        v = row.get("vendor", "other")
        ven = int(v, 16) if isinstance(v, str) and v.lower().startswith("0x") else (
            int(v) if isinstance(v, int) else VENDOR_IDS.get(str(v).lower(), 0))
        out.append({"index": i, "name": str(row.get("name") or f"Fake GPU {i}"), "vendor_id": ven,
                    "device_id": int(row.get("device_id", 0x1000 + i)), "subsys_id": int(row.get("subsys_id", 0)),
                    "vram_mb": int(row.get("vram_mb", 8192)), "shared_mb": int(row.get("shared_mb", 0)),
                    "luid": str(row.get("luid", f"00000000:{0x1000 + i:08x}")),
                    "software": bool(row.get("software", ven == 0x1414))})
    return out


def _cuda_luids() -> list[str] | None:
    """CUDA device number -> LUID ('hhhhhhhh:llllllll', same form as DXGI's) from the NVIDIA driver; None if it can't say."""
    try:
        nv = ctypes.WinDLL("nvcuda.dll")
    except OSError:
        return None
    try:
        if nv.cuInit(0) != 0:
            return None
        n = ctypes.c_int(0)
        if nv.cuDeviceGetCount(ctypes.byref(n)) != 0:
            return None
        out = []
        for i in range(n.value):
            dev = ctypes.c_int(0)
            luid = (ctypes.c_ubyte * 8)()
            mask = ctypes.c_uint(0)
            if nv.cuDeviceGet(ctypes.byref(dev), i) != 0 or nv.cuDeviceGetLuid(luid, ctypes.byref(mask), dev) != 0:
                return None
            low = int.from_bytes(bytes(luid[:4]), "little")
            high = int.from_bytes(bytes(luid[4:]), "little")
            out.append(f"{high:08x}:{low:08x}")
        return out
    except (OSError, AttributeError):
        return None


def _finish(raw: list[dict], source: str) -> list[dict]:
    """Vendor, key, integrated flag and the CUDA device number on top of the raw rows."""
    seen: dict[str, int] = {}
    out = []
    for a in raw:
        vendor = VENDORS.get(a["vendor_id"], "other")
        if a.get("software"):
            vendor = "software"
        base = f"{a['vendor_id']:04x}-{a['device_id']:04x}-{a['subsys_id']:08x}"
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append({**a, "vendor": vendor, "key": f"{base}-{n}",
                    "integrated": vendor in ("amd", "intel") and (
                        (a.get("vram_mb", 0) < INTEGRATED_MB and not a.get("vram_capped"))
                        or a["name"].lower() in _IGPU_NAMES),
                    "cuda_index": None, "source": source})
    nv = [a for a in out if a["vendor"] == "nvidia"]
    if len(nv) == 1:
        nv[0]["cuda_index"] = 0
    elif len(nv) > 1:
        luids = _cuda_luids() if source == "dxgi" else None
        for rank, a in enumerate(nv):
            a["cuda_index"] = luids.index(a["luid"]) if luids and a["luid"] in luids else rank
    return out


def adapters(refresh: bool = False) -> list[dict]:
    """Every graphics adapter (software ones included, flagged), cached for the process. Never raises."""
    with _lock:
        if _cache["list"] is not None and not refresh:
            return [dict(a) for a in _cache["list"]]
        t0 = time.monotonic()
        raw, source = None, ""
        fake = os.environ.get(FAKE_ENV)
        if fake is not None:
            try:
                raw, source = _fake(fake), "fake"
            except (ValueError, TypeError) as e:
                logger.warning("[GPU] %s ignored: %s", FAKE_ENV, e)
        if raw is None and os.name == "nt":
            try:
                raw, source = _dxgi(), "dxgi"
            except (OSError, AttributeError, ValueError) as e:
                logger.warning("[GPU] DXGI adapter list failed (%s) -- asking WMI", e)
            if raw is None:
                try:
                    raw, source = _wmi(), "wmi"
                except (OSError, subprocess.SubprocessError, ValueError) as e:
                    logger.warning("[GPU] WMI adapter list failed: %s", e)
        if raw is None:
            raw, source = [], "none"
        lst = _finish(raw, source)
        _cache.update(list=lst, source=source, t=time.time())
        logger.info("[GPU] adapters source=%s n=%d %.2fs %s", source, len(lst), time.monotonic() - t0,
                    "; ".join(f"#{a['index']} {a['vendor']} {_ascii(a['name'])} {a['vram_mb']}MB" for a in lst) or "-")
        return [dict(a) for a in lst]


def _ascii(s: str) -> str:
    return s.encode("ascii", "replace").decode("ascii")


def source() -> str:
    """dxgi | wmi | fake (FAKE_ENV) | test (install()) | none (nothing could be asked: the old NVIDIA-first
    behavior stands)."""
    adapters()
    return _cache["source"]


def reset_cache() -> None:
    with _lock:
        _cache.update(list=None, source="", t=0.0)


def install(rows: list[dict], source_name: str = "test") -> list[dict]:
    """Replace the card list with `rows` (the FAKE_ENV format) without touching the environment (test fixtures)."""
    lst = _finish(_fake(json.dumps(rows)), source_name)
    with _lock:
        _cache.update(list=lst, source=source_name, t=time.time())
    return [dict(a) for a in lst]


# -- roles -------------------------------------------------------------------------------------------------------
def configure(cfg) -> None:
    """Remember the live settings object (core/jobs.py does this): `hardware.leave_alone` is read on every call."""
    _cfg_ref[:] = [cfg]


def _leave_alone(cfg=None) -> set[str]:
    cfg = cfg if cfg is not None else (_cfg_ref[0] if _cfg_ref else None)
    hw = getattr(cfg, "hardware", None)
    return set(getattr(hw, "leave_alone", None) or [])


def hardware(refresh: bool = False) -> list[dict]:
    """The real cards (no software adapter)."""
    return [a for a in adapters(refresh) if a["vendor"] != "software"]


def usable(cfg=None) -> list[dict]:
    """The cards WardogClips may use, best first: dedicated cards before built-in graphics, then the Direct3D order."""
    off = _leave_alone(cfg)
    out = [a for a in hardware() if a["key"] not in off]
    return sorted(out, key=lambda a: (a["integrated"], a["index"]))


def pool_keys(cfg=None) -> list[str]:
    """One slot pool per usable dedicated card; built-in graphics get a pool only when no dedicated card is usable
    (a job on built-in graphics still reads its HUD text and speech on the dedicated card, so a pool there would
    put twice `jobs.gpu_parallel` jobs' OCR on that card). None usable -> one CPU pool (video jobs still run,
    decoding on the CPU)."""
    use = usable(cfg)
    dedicated = [a for a in use if not a["integrated"]]
    return [a["key"] for a in (dedicated or use)] or [CPU]


def find(key: str | None) -> dict | None:
    if not key:
        return None
    return next((a for a in adapters() if a["key"] == key), None)


@contextlib.contextmanager
def bound(key: str | None):
    """Run the block on this card (its key, or CPU): `current()` answers it on this thread."""
    prev = getattr(_tls, "key", None)
    _tls.key = key
    try:
        yield
    finally:
        _tls.key = prev


def bound_key() -> str | None:
    return getattr(_tls, "key", None)


def bind(key: str | None) -> str | None:
    """Bind this thread to a card (the job queue, for a whole job); returns the previous binding to restore."""
    prev = getattr(_tls, "key", None)
    _tls.key = key
    return prev


def current(cfg=None) -> dict | None:
    """The card this thread's work runs on: the bound one (a job's card), else the best usable card; None = CPU."""
    key = getattr(_tls, "key", None)
    if key == CPU:
        return None
    lst = usable(cfg)
    if key:
        a = next((x for x in lst if x["key"] == key), None)
        if a is not None:
            return a
    return lst[0] if lst else None


def label(a: dict | None) -> str:
    """A card's name for log lines / job rows (driver names already carry the brand)."""
    if a is None:
        return "CPU"
    return _ascii(a["name"] or VENDOR_NAMES.get(a["vendor"], "GPU"))


# -- capabilities ------------------------------------------------------------------------------------------------
def _hw() -> dict:
    try:
        from media import ffmpeg
        return ffmpeg.hw_info()
    except Exception:  # noqa: BLE001
        return {}


def capabilities(a: dict, hw: dict | None = None) -> dict:
    hw = hw if hw is not None else _hw()
    v = a["vendor"]
    if v == "software":
        return {"decode": None, "encode": None, "speech": False, "ocr": False}
    if v == "nvidia" and hw.get("cuda"):
        dec = "cuda"
    elif hw.get("d3d11va", True):        # an older inventory without the key: every Windows ffmpeg build has it
        dec = "d3d11va"
    else:
        dec = None
    enc = ENCODERS.get(v)
    return {"decode": dec, "encode": enc if enc and hw.get(enc) else None, "speech": v == "nvidia",
            "ocr": not a.get("integrated")}


def decode_args(a: dict | None = "current", hw: dict | None = None) -> list[str]:
    """ffmpeg input options for hardware decoding on card `a` (default: this thread's card); [] = decode on the CPU.
    NVIDIA: NVDEC (`-hwaccel cuda -hwaccel_device <cuda n>`); AMD / Intel / other: `-hwaccel d3d11va -hwaccel_device
    <Direct3D index>`. When no card list could be read at all, the old `-hwaccel cuda` try stands (CPU fallback)."""
    hw = hw if hw is not None else _hw()
    if a == "current":
        if source() == "none":
            return ["-hwaccel", "cuda"] if hw.get("cuda") else []
        a = current()
    if a is None:
        return []
    dec = capabilities(a, hw)["decode"]
    if dec == "cuda":
        return ["-hwaccel", "cuda", "-hwaccel_device", str(a["cuda_index"] or 0)]
    if dec == "d3d11va":
        return ["-hwaccel", "d3d11va", "-hwaccel_device", str(a["index"])]
    return []


def decode_label(args: list[str]) -> str:
    """nvdec | d3d11va | cpu for a decode_args() result (log lines)."""
    if "cuda" in args:
        return "nvdec"
    return "d3d11va" if "d3d11va" in args else "cpu"


def whisper_device(cfg=None) -> tuple[str, int]:
    """(device, index) for faster-whisper: the first usable NVIDIA card's CUDA number, else the CPU. No card list at all
    -> CUDA 0 as before (the CPU fallback still catches a PC without CUDA)."""
    if source() == "none":
        return "cuda", 0
    a = next((x for x in usable(cfg) if x["vendor"] == "nvidia"), None)
    return ("cuda", int(a["cuda_index"] or 0)) if a else ("cpu", 0)


def ocr_device_id(cfg=None, cuda: bool = False) -> int | None:
    """DirectML device_id (Direct3D index) for GPU OCR: this thread's card when it can, else the best usable card.
    None = no card may be used (OCR stays on the CPU). No card list at all -> 0 (the default adapter, as before).
    cuda=True: onnxruntime's CUDA provider instead of DirectML -> an NVIDIA card's CUDA number."""
    if source() == "none":
        return 0
    a = current(cfg)
    if a is None:
        return None
    if not capabilities(a)["ocr"] or (cuda and a["vendor"] != "nvidia"):
        a = next((x for x in usable(cfg) if capabilities(x)["ocr"] and (not cuda or x["vendor"] == "nvidia")), None)
    if a is None:
        return None
    return int(a["cuda_index"] or 0) if cuda else int(a["index"])


def ocr_device_ids(cfg=None, cuda: bool = False) -> set[int]:
    """Every device_id GPU OCR may run on now (the OCR pool drops idle engines built for any other card)."""
    if source() == "none":
        return {0}
    return {int(a["cuda_index"] or 0) if cuda else int(a["index"]) for a in usable(cfg)
            if capabilities(a)["ocr"] and (not cuda or a["vendor"] == "nvidia")}


def settings():
    """The live settings object configure() was given (None before)."""
    return _cfg_ref[0] if _cfg_ref else None


def nvenc_args(cfg=None) -> list[str]:
    """`-gpu <cuda n>` for NVENC when the PC has 2+ NVIDIA cards (this thread's card if NVIDIA, else the first usable
    NVIDIA card); [] otherwise (NVENC's default card)."""
    nv = [a for a in hardware() if a["vendor"] == "nvidia"]
    if len(nv) < 2:
        return []
    a = current(cfg)
    if a is None or a["vendor"] != "nvidia":
        a = next((x for x in usable(cfg) if x["vendor"] == "nvidia"), None)
    return ["-gpu", str(a["cuda_index"] or 0)] if a is not None else []


def nvidia_present() -> bool:
    return any(a["vendor"] == "nvidia" for a in hardware())


def vendor(cfg=None) -> str | None:
    """Vendor of this thread's card (nvidia | amd | intel | other), None = no card in use.

    TODO(gpu_guard): vision/gpu_guard.py reads the GPU's busy level from nvidia-smi only. When vendor() is not
    'nvidia' (or nvidia-smi is missing) the guard must not take "no reading" as "the GPU is free": call
    `busy_reading_available()` and, while it is False, keep OCR on the CPU in `auto` (a vendor-neutral reading from
    the Windows "GPU Engine" counters, summed over the whole GPU, never per process, belongs here later)."""
    a = current(cfg)
    return a["vendor"] if a else None


def busy_reading_available(cfg=None) -> bool:
    """Can the app tell how busy the card is right now? Today only on NVIDIA (nvidia-smi). See vendor()."""
    return vendor(cfg) == "nvidia"


# -- the page ----------------------------------------------------------------------------------------------------
def summary(cfg=None) -> dict:
    """GET /api/system/gpus: every card with its role and what it can do."""
    hw = _hw()
    off = _leave_alone(cfg)
    pools = set(pool_keys(cfg))
    rows = []
    for a in adapters():
        if a["vendor"] == "software":
            continue
        rows.append({"key": a["key"], "index": a["index"], "name": a["name"], "vendor": a["vendor"],
                     "vendor_name": VENDOR_NAMES.get(a["vendor"], "Other"), "vram_mb": a["vram_mb"],
                     "integrated": a["integrated"], "luid": a["luid"], "cuda_index": a["cuda_index"],
                     "role": "leave" if a["key"] in off else "use", "video_jobs": a["key"] in pools,
                     "capabilities": capabilities(a, hw)})
    use = usable(cfg)
    return {"source": source(), "gpus": rows, "nvidia": nvidia_present(),
            "default": use[0]["key"] if use else None,
            "whisper": "cuda:%d" % whisper_device(cfg)[1] if whisper_device(cfg)[0] == "cuda" else "cpu",
            "unknown_keys": sorted(k for k in off if not any(r["key"] == k for r in rows))}
