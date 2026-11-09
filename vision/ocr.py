"""OCR over HUD crops: RapidOCR (PP-OCR models on onnxruntime, pip-only, CPU is plenty for
540x160 crops). One lazily built engine per process; `read()` returns axis-aligned boxes.

Both package generations are handled: `rapidocr_onnxruntime` (result = list of
[quad, text, score]) and `rapidocr` 2.x (result object with boxes / txts / scores).
"""

import logging
import threading
import time
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger("wclips.ocr")


@dataclass
class Box:
    text: str
    conf: float
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def h(self) -> int:
        return self.y1 - self.y0


def available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        try:
            import rapidocr  # noqa: F401
            return True
        except Exception:
            return False


# GPU OCR: the same PP-OCR models on the GPU through onnxruntime's DirectML build (`onnxruntime-directml`, loaded
# from venv\ort-directml by utils/ortdml.py -- the venv keeps the CPU `onnxruntime` that pip's dependencies want).
# On ticker crops DirectML is roughly 2.5-3x faster than the CPU and reads the same text; onnxruntime-gpu (CUDA) is
# as fast but occasionally reads a crop differently, so DirectML comes first.
# The rest of a GPU read is CPU work (resize to the 736 px short side, box post-processing). The angle classifier stays
# on the CPU (it is faster there). Whether the GPU is used at all: vision/gpu_guard.py.
GPU_PROVIDERS = ("DmlExecutionProvider", "CUDAExecutionProvider")
GPU_RETRY_S = 600.0                               # after a GPU failure (a driver reset while gaming): CPU for this long
_gpu_ep: list = []                                # [provider name or None] once probed
# One GPU read at a time, process-wide (both pools): concurrent DirectML reads from parallel detects while whisper
# decodes on CUDA can hang the GPU (DXGI 887A0006 "the GPU will not respond"). A read that cannot get the GPU within
# GPU_WAIT_S reads on the CPU instead (a GPU read is ~80 ms, a CPU read ~240 ms).
GPU_WAIT_S = 0.2
_gpu_gate = threading.Lock()
# A lost device (hung / removed / reset) cannot be used again by this process: every later session build fails.
# Then GPU OCR stays off until the app restarts.
DEVICE_LOST = ("887A0005", "887A0006", "887A0007", "887A0020", "DXGI_ERROR_DEVICE")
GPU_BUILD_TRIES = 2                               # GPU engine builds failing in a row = off until the app restarts
_gpu_lost: list = []                              # [reason] once the GPU is given up for this process
_gpu_state = {"build_fails": 0, "last_fail": None}   # failed GPU builds, counted once per GPU_RETRY_S window


def gpu_lost() -> str | None:
    """Why GPU OCR is off until the app restarts, or None."""
    return _gpu_lost[0] if _gpu_lost else None


def _lose_gpu(why: str) -> None:
    if not _gpu_lost:
        _gpu_lost.append(why)
        logger.warning("[OCR] GPU OCR off until WardogClips restarts (%s) -- restart the app to use the GPU again", why)


def _device_lost(exc: BaseException) -> str | None:
    """The DXGI code when an inference error (or one it was raised from) says the GPU device is gone."""
    seen = 0
    while exc is not None and seen < 8:
        text = str(exc).upper()
        for code in DEVICE_LOST:
            if code in text:
                return code
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return None


def gpu_provider() -> str | None:
    """The GPU execution provider this onnxruntime build offers (DirectML first), or None."""
    if not _gpu_ep:
        try:
            import onnxruntime as ort
            have = ort.get_available_providers()
            _gpu_ep.append(next((p for p in GPU_PROVIDERS if p in have), None))
        except Exception:  # noqa: BLE001
            _gpu_ep.append(None)
    return _gpu_ep[0]


def _session_holder(mod):
    """rapidocr_onnxruntime 1.x keeps its onnxruntime session at <module>.infer.session (det, cls) or
    <module>.session.session (rec)."""
    return getattr(mod, "infer", None) or getattr(mod, "session", None)


def _gpu_device_id() -> int | None:
    """The graphics card GPU OCR runs on (Direct3D index = DirectML device_id): this job's card when it can OCR, else
    the best usable one (utils/gpus.py, Settings > Hardware); None = no card may be used."""
    try:
        from utils import gpus
        return gpus.ocr_device_id(cuda=gpu_provider() == "CUDAExecutionProvider")
    except Exception:  # noqa: BLE001 - the card list is a hint: the default card as before
        return 0


def _gpu_device_ids() -> set[int]:
    """Every device_id GPU OCR may use now (Settings > Hardware); idle engines on any other card are released."""
    try:
        from utils import gpus
        return gpus.ocr_device_ids(cuda=gpu_provider() == "CUDAExecutionProvider")
    except Exception:  # noqa: BLE001
        return {0}


def _to_gpu(eng, device_id: int = 0) -> bool:
    """Move the detector + recognizer sessions of a built engine to the GPU provider on card device_id. False when
    not possible (another package generation, no GPU provider, the session will not build) -- the engine is then left
    as it was."""
    ep = gpu_provider()
    if ep is None:
        return False
    try:
        import onnxruntime as ort
        new = []
        for part in ("text_detector", "text_recognizer"):
            holder = _session_holder(getattr(eng, part, None))
            sess = getattr(holder, "session", None)
            path = getattr(sess, "_model_path", None)
            if holder is None or not path:
                return False
            so = ort.SessionOptions()
            so.log_severity_level = 3
            so.enable_mem_pattern = False                    # DirectML wants these two
            so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            s = ort.InferenceSession(path, sess_options=so, providers=[(ep, {"device_id": str(int(device_id))}),
                                                                       "CPUExecutionProvider"])
            if s.get_providers()[0] != ep:
                return False
            new.append((holder, s))
        for holder, s in new:
            holder.session = s
        return True
    except Exception:  # noqa: BLE001 - the CPU engine still works
        logger.warning("[OCR] GPU session failed", exc_info=True)
        return False


class Ocr:
    """A small pool of lazily built engines (detect jobs run in parallel, one per free drive
    slot). A RapidOCR instance is used by one thread at a time; up to `max_engines`
    are built on demand, so two detects OCR side by side instead of queueing on one lock.
    Two pools: 'cpu' and 'gpu' (read_ok(..., gpu=True)); a GPU engine that cannot be built or fails
    an inference switches this Ocr to the CPU for the rest of the process (`gpu_failed`).
    `calls` / `seconds` are process-wide totals (updated under a lock)."""

    def __init__(self, max_engines: int = 3, fast: bool = False):
        self.fast = fast
        self.max_engines = max(1, int(max_engines))
        self._cv = threading.Condition()
        self._free: dict[str, list] = {"cpu": [], "gpu": []}
        self._built = {"cpu": 0, "gpu": 0}
        self._building = {"cpu": 0, "gpu": 0}
        self.gpu_failed = False
        self._gpu_off_until = 0.0
        self._gpu_gen = 0                         # bumped by drop_gpu: engines of an older generation are thrown away
        self._tl = threading.local()              # the device THIS thread's last read really ran on
        self.calls = 0
        self.seconds = 0.0
        self.gpu_calls = 0

    def gpu_ready(self) -> bool:
        if self.gpu_failed and time.monotonic() >= self._gpu_off_until:
            self.gpu_failed = False               # try the GPU again after the cooldown
        return (not self.gpu_failed and not _gpu_lost and gpu_provider() is not None
                and _gpu_device_id() is not None)

    def last_device(self) -> str:
        return getattr(self._tl, "dev", "cpu")

    def drop_gpu(self) -> None:
        """Release the GPU engines (their DirectML sessions hold video memory): idle ones now, busy ones when they come
        back. Called when the guard moves OCR to the CPU (a game started) and after a GPU failure."""
        with self._cv:
            self._gpu_gen += 1
            n = len(self._free["gpu"])
            self._built["gpu"] -= n
            self._free["gpu"].clear()
            self._cv.notify_all()
        if n:
            logger.info("[OCR] released %d GPU engine(s)%s", n, " (fast)" if self.fast else "")

    def set_max_engines(self, n: int) -> None:
        with self._cv:
            self.max_engines = max(1, int(n))
            self._cv.notify_all()

    def _build(self, dev: str = "cpu"):
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            from rapidocr import RapidOCR
        t0 = time.monotonic()
        eng = _fast_engine(RapidOCR) if self.fast else None
        if eng is None:
            eng = RapidOCR()
        devid = None
        if dev == "gpu":
            devid = _gpu_device_id()
            if devid is None:
                raise _NoGpu("no graphics card may be used (Settings > Hardware)")
            if not _to_gpu(eng, devid):
                # one count per cooldown window, process-wide: the two pools (ocr, ocr_fast) each have their own
                # cooldown, so without this a feed read's build right after a failed ticker build counted twice
                now = time.monotonic()
                last = _gpu_state.get("last_fail")
                if last is None or now - last >= GPU_RETRY_S * 0.9:
                    _gpu_state["build_fails"] += 1
                    _gpu_state["last_fail"] = now
                if _gpu_state["build_fails"] >= GPU_BUILD_TRIES:
                    _lose_gpu(f"the GPU engine could not be built {_gpu_state['build_fails']}x in a row")
                raise _NoGpu("the GPU engine could not be built")
            _gpu_state["build_fails"], _gpu_state["last_fail"] = 0, None
        eng._wclips_dev = dev
        eng._wclips_devid = devid
        eng._wclips_gen = self._gpu_gen
        logger.info("[OCR] engine ready in %.1fs engines=%d kind=%s device=%s", time.monotonic() - t0,
                    self._built[dev] + 1, "fast" if self.fast and getattr(eng, "_wclips_fast", False) else "full",
                    f"{gpu_provider()}:{devid}" if dev == "gpu" else "cpu")
        return eng

    def _acquire(self, dev: str = "cpu"):
        with self._cv:
            while True:
                if dev == "gpu" and (self.gpu_failed or _gpu_lost):
                    raise _NoGpu("GPU OCR is off after an error")
                if dev == "gpu" and self._free["gpu"]:
                    allowed = _gpu_device_ids()
                    for e in [e for e in self._free["gpu"] if getattr(e, "_wclips_devid", 0) not in allowed]:
                        self._free["gpu"].remove(e)          # its card is left alone now (Settings > Hardware)
                        self._built["gpu"] -= 1
                    want = _gpu_device_id()
                    match = [e for e in self._free["gpu"] if getattr(e, "_wclips_devid", 0) == want]
                    if match:
                        self._free["gpu"].remove(match[-1])
                        return match[-1]
                    if self._free["gpu"] and self._built["gpu"] + self._building["gpu"] >= self.max_engines:
                        self._free["gpu"].pop(0)             # an idle engine on another card makes room for this one
                        self._built["gpu"] -= 1
                    if self._built["gpu"] + self._building["gpu"] < self.max_engines:
                        self._building["gpu"] += 1
                        break
                    self._cv.wait(1.0)
                    continue
                if self._free[dev]:
                    return self._free[dev].pop()
                if self._built[dev] + self._building[dev] < self.max_engines:
                    self._building[dev] += 1
                    break
                self._cv.wait(1.0)
        try:
            eng = self._build(dev)                  # outside the lock: building takes a second or two
        except BaseException:
            with self._cv:
                self._building[dev] -= 1
                self._cv.notify_all()
            raise
        with self._cv:
            self._building[dev] -= 1
            self._built[dev] += 1
        return eng

    def _give_back(self, eng, broken: bool = False) -> None:
        dev = getattr(eng, "_wclips_dev", "cpu")
        with self._cv:
            if dev == "gpu" and (broken or self.gpu_failed or _gpu_lost
                                 or getattr(eng, "_wclips_gen", 0) != self._gpu_gen):
                self._built["gpu"] -= 1                       # dropped: its sessions go with it
            else:
                self._free[dev].append(eng)
            self._cv.notify()

    def read(self, img: np.ndarray, min_conf: float = 0.3, gpu: bool = False) -> list[Box]:
        """Boxes with text, best effort; an empty list when nothing is readable."""
        return self.read_ok(img, min_conf, gpu=gpu)[0]

    def _gpu_failed(self, why: str) -> None:
        with self._cv:
            first = not self.gpu_failed
            self.gpu_failed = True
            self._gpu_off_until = time.monotonic() + GPU_RETRY_S
        if first and not _gpu_lost:
            logger.warning("[OCR] GPU OCR off for %.0f min (%s) -- on the CPU meanwhile", GPU_RETRY_S / 60, why)
        self.drop_gpu()

    def read_ok(self, img: np.ndarray, min_conf: float = 0.3, gpu: bool = False) -> tuple[list[Box], bool]:
        """(boxes, ok): ok is False when the inference itself failed, so a caller can tell 'nothing on screen' from
        'not read' (detect never reuses a failed ticker read). gpu=True reads on the GPU pool when it works (a GPU
        that fails falls back to the CPU for this read and for good). Only ONE read runs on the GPU at a time
        (`_gpu_gate`, both pools); a read that cannot get it within GPU_WAIT_S runs on the CPU."""
        if img is None or img.size == 0:
            self._tl.dev = "cpu"
            return [], True
        dev = "gpu" if gpu and self.gpu_ready() else "cpu"
        gated = False
        if dev == "gpu":
            gated = _gpu_gate.acquire(timeout=GPU_WAIT_S)
            if not gated:
                dev = "cpu"                         # another read holds the GPU: this one on the CPU
        broken = False
        try:
            try:
                eng = self._acquire(dev)
            except _NoGpu as e:
                self._gpu_failed(str(e))
                dev, eng = "cpu", self._acquire("cpu")
            try:
                t0 = time.monotonic()
                try:
                    res = eng(img)
                except Exception as e:  # noqa: BLE001 - OCR must never kill a stage
                    if dev == "gpu":
                        logger.warning("[OCR] GPU inference failed", exc_info=True)
                        broken = True
                        code = _device_lost(e)
                        if code:
                            _lose_gpu(f"the GPU stopped responding: DXGI {code}")
                        self._gpu_failed(f"inference error{' ' + code if code else ''}")
                    else:
                        logger.warning("[OCR] inference failed", exc_info=True)
                        self._tl.dev = "cpu"
                        return [], False
                dt = time.monotonic() - t0
            finally:
                self._give_back(eng, broken=broken)
        finally:
            if gated:
                _gpu_gate.release()
        if broken:
            return self.read_ok(img, min_conf, gpu=False)
        with self._cv:
            self.calls += 1
            self.seconds += dt
            if dev == "gpu":
                self.gpu_calls += 1
        self._tl.dev = dev
        return _normalize(res, min_conf), True


class _NoGpu(RuntimeError):
    pass


# The reward ticker's fast engine: RapidOCR's detector scales every crop up to a SHORT side of 736 px (a 120 px
# ticker -> ~2500x736) and runs a 180-degree angle classifier HUD text never needs. Short side 160 + no classifier is
# ~4x faster per ticker crop and usually reads the same money / ticker lines. NOT for the kill feed: there it adds
# stray characters to player names (an extra trailing letter, '1' for 'l'), and the feed is read rarely anyway.
# OPT-IN (detection.ocr_fast_ticker, default off): it misses roughly one event line in ten that the full engine
# reads (larger short sides miss some too), which costs real kills.
FAST_DET_LIMIT = 160


def _fast_engine(RapidOCR):
    """A RapidOCR built with the fast ticker settings, or None (then the caller builds a default engine)."""
    try:
        try:                                        # rapidocr_onnxruntime 1.x: det_* / use_angle_cls keyword args
            eng = RapidOCR(det_limit_side_len=FAST_DET_LIMIT, det_model_path=None, use_angle_cls=False)
        except TypeError:                           # rapidocr 2.x: a params dict
            eng = RapidOCR(params={"Det.limit_side_len": FAST_DET_LIMIT, "Global.use_cls": False})
        eng._wclips_fast = True
        return eng
    except Exception:  # noqa: BLE001 - an unknown package generation: the default engine still works
        logger.warning("[OCR] fast ticker engine unavailable, using the default settings", exc_info=True)
        return None


def _normalize(res, min_conf: float) -> list[Box]:
    out: list[Box] = []
    if res is None:
        return out
    items = res[0] if isinstance(res, tuple) else res           # rapidocr_onnxruntime: (result, elapse)
    if items is None:
        return out
    if hasattr(items, "boxes") and hasattr(items, "txts"):      # rapidocr 2.x
        boxes, txts, scores = items.boxes, items.txts, items.scores
        if boxes is None or txts is None:
            return out
        for quad, text, score in zip(boxes, txts, scores or [1.0] * len(txts)):
            out.append(_box(quad, text, score))
    else:
        for item in items:
            try:
                quad, text, score = item[0], item[1], item[2]
            except (TypeError, IndexError):
                continue
            out.append(_box(quad, text, score))
    out = [b for b in out if b.text and b.conf >= min_conf]
    out.sort(key=lambda b: (b.cy, b.cx))
    return out


def _box(quad, text, score) -> Box:
    pts = np.asarray(quad, dtype=float).reshape(-1, 2)
    return Box(text=str(text).strip(), conf=float(score), x0=int(pts[:, 0].min()), y0=int(pts[:, 1].min()),
               x1=int(pts[:, 0].max()), y1=int(pts[:, 1].max()))


def white_mask(img_bgr: np.ndarray, v_min: int = 200, s_max: int = 60) -> np.ndarray:
    """Pixels that look like HUD text (bright, unsaturated)."""
    import cv2
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    return ((hsv[:, :, 2] >= v_min) & (hsv[:, :, 1] <= s_max)).astype(np.uint8)


def white_count(img_bgr: np.ndarray, y0: int = 0, y1: int | None = None) -> int:
    m = white_mask(img_bgr)
    return int(m[y0:y1].sum())


ocr = Ocr()                     # full settings: kill feed, respawn text
ocr_fast = Ocr(fast=True)       # the reward ticker (detect's keyframe pass)


def _wire_guard() -> None:
    try:
        from vision import gpu_guard
        gpu_guard.guard.on_cpu.extend([ocr.drop_gpu, ocr_fast.drop_gpu])
    except Exception:  # noqa: BLE001
        pass


_wire_guard()
