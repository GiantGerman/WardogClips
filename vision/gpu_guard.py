"""Whether detect's OCR may use the GPU right now.

Settings > Detection > ocr_device: `cpu` never, `gpu` whenever the GPU engine works, `auto` (default) = the GPU only
while nothing else is keeping it busy -- the game usually runs on the same card, and OCR on it during a match costs frames.

How `auto` decides without looking at processes (hard rule: no process enumeration, never the game): the GPU's own
utilization from `nvidia-smi --query-gpu=utilization.gpu` (a GPU-level number, like media/encoder.py's NVENC session
count). Our own GPU OCR shows up in that number too, so every CHECK_S the guard hands out the CPU for a short PROBE_S
window (all detects together: one guard per process), samples the utilization meanwhile, and decides: busy (>=
BUSY_PCT) -> CPU until the next check, else GPU. While our own whisper is transcribing, a sample says nothing about a
game, so the last decision stands. No nvidia-smi (an AMD / Intel card) -> the 3D load from Windows' own GPU counters
(utils/gpuload.py, machine-wide, no process names); neither answering -> the CPU (nothing says the GPU is free).
"""

import logging
import statistics
import subprocess
import threading
import time

logger = logging.getLogger("wclips.gpu")

CHECK_S = 60.0          # re-decide this often while on the CPU
CHECK_GPU_S = 15.0      # ... and this often while on the GPU (a match starting is noticed within ~15 s)
PROBE_S = 1.6           # CPU-only window while sampling (~2.7 % of the time)
SAMPLES = 3
BUSY_PCT = 35           # the HIGHEST sample at or above this = someone else is using it (games: 60-99 %)
_NO_WINDOW = 0x08000000
_sleep = time.sleep          # (tests replace it)


def gpu_utilization() -> int | None:
    """Current GPU utilization in % (first GPU), None when nobody can say. Without nvidia-smi: the busiest
    adapter's 3D load from Windows' performance counters (utils/gpuload.py)."""
    from media.encoder import _smi
    exe = _smi()
    if not exe:
        try:
            from utils import gpuload
            return gpuload.utilization()
        except Exception:  # noqa: BLE001 - no reading is an answer too
            return None
    try:
        cp = subprocess.run([exe, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                            capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
        vals = [int(x.strip()) for x in cp.stdout.splitlines() if x.strip().isdigit()]
        return vals[0] if vals else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _whisper_busy() -> bool:
    """Is our own whisper decoding right now (it holds Engine._lock, an RLock -- RLock.locked() only exists from Python
    3.14, so: try to take it without waiting; held by another thread = busy)."""
    try:
        from stt import whisper
        lk = whisper.engine._lock
        if lk.acquire(blocking=False):
            lk.release()
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


class GpuGuard:
    def __init__(self):
        self._lock = threading.Lock()
        self._use = None               # None = never decided
        self._next_check = 0.0
        self._probe_until = 0.0
        self._probing = False
        self.last_reason = "not checked yet"
        self.on_cpu = []               # callbacks when the decision turns to the CPU (Ocr.drop_gpu: free the VRAM)

    def use_gpu(self, setting: str, gpu_ready: bool) -> bool:
        """Per OCR call: may it run on the GPU? Cheap (a lock and a clock read); the sampling runs on a thread."""
        if setting == "cpu" or not gpu_ready:
            return False
        if setting == "gpu":
            return True
        now = time.monotonic()
        with self._lock:
            if now >= self._next_check and not self._probing:
                self._probing = True
                self._probe_until = now + PROBE_S
                self._next_check = now + (CHECK_GPU_S if self._use else CHECK_S)
                try:
                    threading.Thread(target=self._probe, name="gpu-guard", daemon=True).start()
                except Exception:  # noqa: BLE001 - the guard must never break detect
                    self._probing = False
                    logger.warning("[GPU] probe thread did not start", exc_info=True)
            if self._probing or now < self._probe_until:
                return False
            return bool(self._use)

    def _probe(self) -> None:
        try:
            _sleep(0.4)                              # let our GPU reads in flight finish
            vals = []
            for k in range(SAMPLES):
                v = gpu_utilization()
                if v is None:
                    break
                vals.append(v)
                if k < SAMPLES - 1:
                    _sleep((PROBE_S - 0.4) / SAMPLES)
            whisper = _whisper_busy()
            from media.encoder import _smi
            have_smi = _smi() is not None
            with self._lock:
                old = self._use
                if not vals and not have_smi:
                    # neither nvidia-smi nor the Windows GPU counters answered: nothing says the GPU is free
                    self._use, why = False, "GPU load unknown (no nvidia-smi, no GPU counters) -- CPU"
                elif not vals:
                    # nvidia-smi is there but did not answer -- most likely while a game loads the driver
                    self._use = old if old is not None else False
                    why = f"nvidia-smi did not answer -- keeping {'GPU' if self._use else 'CPU'}"
                elif whisper and old is not None:
                    why = f"our transcribe is on the GPU ({max(vals)} %) -- keeping {'GPU' if old else 'CPU'}"
                else:
                    peak = max(vals)
                    self._use = peak < BUSY_PCT
                    why = f"GPU up to {peak:.0f} % busy -- {'GPU' if self._use else 'CPU (something else is using the GPU)'}"
                self.last_reason = why
                changed = old != self._use
                if changed:
                    logger.info("[GPU] OCR on the %s: %s", "GPU" if self._use else "CPU", why)
                    self._next_check = time.monotonic() + (CHECK_GPU_S if self._use else CHECK_S)
            if changed and not self._use:
                for cb in list(self.on_cpu):
                    try:
                        cb()
                    except Exception:  # noqa: BLE001
                        logger.warning("[GPU] release callback failed", exc_info=True)
        except Exception:  # noqa: BLE001 - the guard must never break detect
            logger.warning("[GPU] probe failed", exc_info=True)
            with self._lock:
                if self._use is None:
                    self._use = False
        finally:
            with self._lock:
                self._probing = False


guard = GpuGuard()
