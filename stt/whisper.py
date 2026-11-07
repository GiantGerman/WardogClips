"""Local speech-to-text: faster-whisper on CUDA, one model resident at a time.

- CUDA DLLs: ctranslate2's Windows wheel does not bundle the CUDA runtime; cublas64_12 /
  cudnn come from the nvidia-cublas-cu12 + nvidia-cudnn-cu12 pip wheels (the speech-engine
  add-on). `add_dll_directory` alone is NOT enough -- ctranslate2 resolves cublas through a
  plain LoadLibrary that searches PATH, so the wheel dirs are prepended there too.
- A CUDA failure can surface lazily on the first encode (the model LOADS fine without
  cublas), so `load()` warms the model on a second of silence and falls back to CPU int8.
- Decode hardening against hallucination: never condition on previous text, a short
  temperature ladder, log-prob gate, and `is_junk()` for the leftovers.
"""

import logging
import os
import re
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("wclips.stt")

JUNK_TEXTS = {"thank you.", "thanks for watching.", "thanks for watching!", "bye.", "you", "thank you", "."}


CUDA_SUBDIRS = ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime")
_wired: list[str] = []          # dirs wire_cuda_dlls() added (cuda_ready() looks only there)
_dll_handles: list = []         # add_dll_directory cookies: dropping one would un-register its dir


def cuda_dll_dirs() -> list[Path]:
    """Every `nvidia/<lib>/bin` dir the CUDA DLLs may come from: the pip wheels in this Python's site-packages (a
    source run's venv, the installer's runtime/Lib/site-packages) and the downloaded cuda pack
    (`<DATA_DIR>/packs/cuda/nvidia` -- the installer ships without the 1 GB of CUDA libs)."""
    bases = [Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"]
    try:
        from utils import paths
        bases.append(paths.PACKS_DIR / "cuda" / "nvidia")
    except Exception:  # noqa: BLE001 - paths must never stop whisper from loading
        pass
    out = []
    for base in bases:
        for sub in CUDA_SUBDIRS:
            d = base / sub / "bin"
            if d.is_dir():
                out.append(d)
    return out


def wire_cuda_dlls() -> list[str]:
    """add_dll_directory AND a PATH prepend for every CUDA dir found (ctranslate2 loads cublas through a plain
    LoadLibrary that searches PATH). Idempotent; safe to call again after the cuda pack installs. Returns the dirs."""
    for d in cuda_dll_dirs():
        sd = str(d)
        if sd in _wired:
            continue
        try:
            _dll_handles.append(os.add_dll_directory(sd))
        except (OSError, AttributeError):
            pass
        if sd not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = sd + os.pathsep + os.environ.get("PATH", "")
        _wired.append(sd)
        logger.debug("[STT] cuda dll dir wired dir=%s", sd)
    return list(_wired)


def cuda_ready() -> bool:
    """cublas64_12.dll AND a cudnn64_9.dll are in the wired dirs (= whisper can run on CUDA, given an NVIDIA GPU).
    Wires first, so it answers for what is on disk right now."""
    wire_cuda_dlls()
    have_cublas = have_cudnn = False
    for sd in _wired:
        d = Path(sd)
        have_cublas = have_cublas or (d / "cublas64_12.dll").exists()
        try:
            have_cudnn = have_cudnn or any(d.glob("cudnn64_9*.dll"))
        except OSError:
            pass
    return have_cublas and have_cudnn


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def cached_models() -> list[str]:
    """Model names already in the Hugging Face cache (no download needed)."""
    hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    out = []
    try:
        for p in hub.iterdir():
            # Systran/faster-whisper-<name>; large-v3-turbo lives under mobiuslabsgmbh/faster-whisper-large-v3-turbo
            if p.name.startswith("models--") and "--faster-whisper-" in p.name:
                out.append(p.name.split("--faster-whisper-", 1)[1])
    except OSError:
        pass
    return sorted(set(out))


def is_cached(name: str) -> bool:
    """True when the repo faster-whisper will load for `name` is in the HF cache with a snapshot. cached_models()
    lists any org's faster-whisper-* folder (for display); loading with local_files_only needs the EXACT repo
    (medium.en = Systran/..., large-v3-turbo = mobiuslabsgmbh/...), else a look-alike folder would make an
    uncached model refuse to download."""
    try:
        from faster_whisper.utils import _MODELS
        repo = name if "/" in name else _MODELS.get(name)
    except Exception:  # noqa: BLE001 - an older / newer package layout: fall back to the name list
        return name in cached_models()
    if not repo:
        return False
    try:
        # a snapshot folder alone is not enough -- an interrupted download leaves one, and loading it with
        # local_files_only then fails. core.packs knows what a complete snapshot is.
        from core import packs
        return packs.snapshot_complete(repo)
    except Exception:  # noqa: BLE001 - fall back to the old folder test
        pass
    hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    snaps = hub / ("models--" + repo.replace("/", "--")) / "snapshots"
    try:
        return any(snaps.iterdir())
    except OSError:
        return False


_WORD = re.compile(r"[a-z0-9']+")
_STUTTER = re.compile(r"^(\w{1,4}?)\1{7,}\w{0,4}$")      # "hehehehehehe...": one syllable on repeat


def is_junk(seg: dict, hotwords: str | list[str] | None = None) -> bool:
    """Segments the model itself scored as nothing, the classic silence hallucinations, and
    echoes of the decoder's hotwords: on a track with no speech whisper writes the prompt back
    ("PlayerOne, SquadMate" -- the player + squad names -- on every 30 s window of a silent mic)."""
    text = (seg.get("text") or "").strip().lower()
    if not text:
        return True
    if seg.get("no_speech_prob", 0) > 0.85 and seg.get("avg_logprob", 0) < -1.0:
        return True
    if seg.get("compression_ratio", 0) > 2.6:
        return True
    if text in JUNK_TEXTS and (seg["t1"] - seg["t0"]) < 2.0:
        return True
    words = _WORD.findall(text)
    if hotwords and words and seg.get("no_speech_prob", 0) >= 0.5:
        hot = hotwords if isinstance(hotwords, str) else " ".join(hotwords)
        if set(words) <= set(_WORD.findall(hot.lower())):
            return True
    if len(words) == 1 and len(words[0]) >= 16 and _STUTTER.match(words[0]):
        return True
    return False


def split_at_gaps(seg: dict, gap_s: float = 2.5) -> list[dict]:
    """Whisper can span a silence inside one segment (e.g. two sentences 150 s apart in one
    segment), so a line's timestamp would lie. With word timestamps, cut wherever the gap
    between two words exceeds `gap_s`; each piece gets its own honest t0 / t1."""
    words = seg.get("words") or []
    if len(words) < 2:
        return [seg]
    pieces, cur = [], [words[0]]
    for w in words[1:]:
        if w["t0"] - cur[-1]["t1"] > gap_s:
            pieces.append(cur)
            cur = [w]
        else:
            cur.append(w)
    pieces.append(cur)
    if len(pieces) == 1:
        return [seg]
    out = []
    for ws in pieces:
        text = " ".join(w["w"] for w in ws).strip()
        if not text:
            continue
        out.append({**seg, "t0": ws[0]["t0"], "t1": ws[-1]["t1"], "text": text, "words": ws})
    return out or [seg]


class Engine:
    def __init__(self):
        self._lock = threading.RLock()
        self._model = None
        self._key: tuple[str, str] | None = None
        self.device = ""
        self.loaded_utc: float | None = None
        self.last_error: str | None = None
        self.reload_pending = False      # reload_engine() while a decode held the lock: dropped at the next load()

    # -- lifecycle ----------------------------------------------------------------
    def load(self, name: str, compute: str = "float16"):
        key = (name, compute)
        with self._lock:
            if self.reload_pending:
                self.reload_pending = False
                self.unload()
            if self._model is not None and self._key == key:
                return self._model
            self.unload()
            wire_cuda_dlls()
            import numpy as np
            from faster_whisper import WhisperModel
            t0 = time.monotonic()
            model, dev = None, ""
            # a cached model loads without touching huggingface.co (works offline); an uncached
            # one downloads on first use
            local_only = is_cached(name)
            try:
                model = WhisperModel(name, device="cuda", compute_type=compute, local_files_only=local_only)
                dev = f"cuda/{compute}"
                list(model.transcribe(np.zeros(16000, np.float32), beam_size=1, vad_filter=False,
                                      without_timestamps=True, language="en")[0])
            except Exception as e:  # noqa: BLE001 - any CUDA trouble means CPU fallback
                self.last_error = f"cuda: {type(e).__name__}: {str(e)[:160]}"
                logger.warning("[STT] CUDA load failed (%s) -- CPU int8 fallback", self.last_error)
                model = WhisperModel(name, device="cpu", compute_type="int8", local_files_only=local_only)
                dev = "cpu/int8"
            self._model, self._key, self.device = model, key, dev
            self.loaded_utc = time.time()
            logger.info("[STT] model %s loaded on %s in %.1fs", name, dev, time.monotonic() - t0)
            return model

    def unload(self) -> None:
        with self._lock:
            if self._model is not None:
                logger.info("[STT] model %s unloaded", self._key[0] if self._key else "?")
            self._model = None
            self._key = None
            self.device = ""

    def status(self) -> dict:
        return {"available": available(), "loaded": self._key[0] if self._key else None,
                "compute": self._key[1] if self._key else None, "device": self.device,
                "loaded_utc": self.loaded_utc, "last_error": self.last_error, "cached_models": cached_models(),
                "cuda_ready": cuda_ready()}

    # -- work ---------------------------------------------------------------------
    def transcribe(self, wav_path, *, model: str, compute: str = "float16", hotwords: str = "",
                   word_timestamps: bool = True, vad: bool = True, language: str = "en", offset_s: float = 0.0,
                   batched: bool = False, batch_size: int = 8):
        """Yields one dict per segment: t0, t1, text, words [{w, t0, t1, p}], avg_logprob,
        no_speech_prob, compression_ratio. `offset_s` (a resumed transcribe) starts that far into the wav; the
        times stay those of the whole file. `batched` = faster-whisper's BatchedInferencePipeline (speech chunks
        of <= 30 s decoded `batch_size` at a time; see _batched)."""
        if batched:
            yield from self._batched(wav_path, model=model, compute=compute, hotwords=hotwords,
                                     word_timestamps=word_timestamps, vad=vad, language=language,
                                     offset_s=offset_s, batch_size=batch_size)
            return
        m = self.load(model, compute)
        # Decode the wav ourselves (wave + numpy): faster-whisper's decoder goes through PyAV and
        # breaks on av version drift (e.g. open() got an unexpected keyword
        # argument 'metadata_errors'); a float32 array takes the same path as the warm-up.
        from media.audio import read_wav
        audio = read_wav(wav_path)
        off = max(0.0, float(offset_s or 0.0))
        if off:
            from media.audio import SAMPLE_RATE
            audio = audio[int(off * SAMPLE_RATE):]
        kw = dict(language=language or None, beam_size=5, vad_filter=vad,
                  vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
                  word_timestamps=word_timestamps, condition_on_previous_text=False,
                  temperature=[0.0, 0.2, 0.4], log_prob_threshold=-1.0, no_speech_threshold=0.6)
        if hotwords:
            kw["hotwords"] = hotwords
        with self._lock:
            segments, _info = m.transcribe(audio, **kw)
            for s in segments:
                yield _seg_dict(s, off, word_timestamps)

    def _batched(self, wav_path, *, model, compute, hotwords, word_timestamps, vad, language, offset_s, batch_size):
        """BatchedInferencePipeline (faster-whisper 1.2.1). What it does differently from the sequential path:
        - the audio is cut into speech chunks first (Silero VAD, merged up to 30 s) and those are decoded
          `batch_size` at a time; with vad off there are no chunks, so we hand it fixed 30 s windows
          (clip_timestamps; its own vad-off path refuses audio longer than 30 s);
        - only the first temperature is used (no fallback ladder) and no window is skipped as silent -- so the
          sequential path's silence gate (no_speech_prob > 0.6 AND avg_logprob < -1.0) is applied here per
          segment, and is_junk() catches the rest as before;
        - condition_on_previous_text is always off (what we use anyway); hotwords are supported."""
        m = self.load(model, compute)
        from faster_whisper import BatchedInferencePipeline
        from media.audio import SAMPLE_RATE, read_wav
        audio = read_wav(wav_path)
        off = max(0.0, float(offset_s or 0.0))
        if off:
            audio = audio[int(off * SAMPLE_RATE):]
        dur = len(audio) / SAMPLE_RATE
        if dur <= 0.05:
            return
        kw = dict(language=language or None, beam_size=5, word_timestamps=word_timestamps,
                  without_timestamps=False, condition_on_previous_text=False, temperature=0.0,
                  log_prob_threshold=-1.0, no_speech_threshold=0.6, batch_size=max(1, int(batch_size)))
        if vad:
            kw["vad_filter"] = True
            kw["vad_parameters"] = {"min_silence_duration_ms": 500, "speech_pad_ms": 200}
        else:
            kw["vad_filter"] = False
            kw["clip_timestamps"] = [{"start": round(a, 3), "end": round(min(dur, a + 30.0), 3)}
                                     for a in _windows(dur, 30.0)]
        if hotwords:
            kw["hotwords"] = hotwords
        with self._lock:
            pipe = BatchedInferencePipeline(model=m)
            segments, _info = pipe.transcribe(audio, **kw)
            for s in segments:
                if float(s.no_speech_prob) > 0.6 and float(s.avg_logprob) < -1.0:
                    continue                    # the sequential decoder skips such a window as silence
                yield _seg_dict(s, off, word_timestamps)


def _windows(dur: float, step: float) -> list[float]:
    out, a = [], 0.0
    while a < dur - 0.05:
        out.append(a)
        a += step
    return out


def _seg_dict(s, off: float, word_timestamps: bool) -> dict:
    words = []
    for w in (s.words or []) if word_timestamps else []:
        words.append({"w": w.word.strip(), "t0": round(off + float(w.start), 2),
                      "t1": round(off + float(w.end), 2), "p": round(float(w.probability), 3)})
    return {"t0": round(off + float(s.start), 2), "t1": round(off + float(s.end), 2), "text": s.text.strip(),
            "words": words, "avg_logprob": round(float(s.avg_logprob), 3),
            "no_speech_prob": round(float(s.no_speech_prob), 3),
            "compression_ratio": round(float(s.compression_ratio), 3)}


engine = Engine()


def reload_engine() -> bool:
    """Drop the resident model so the next transcribe loads it again (after the cuda pack installed: the reload then
    finds the CUDA DLLs and runs on the GPU instead of the CPU fallback). Re-wires the DLL dirs first. Safe when no
    model is loaded. Never blocks: transcribe holds the engine lock for the whole decode (tens of minutes on a long
    VOD), so while one runs the drop is only flagged and happens at the next load() -- the add-on install reports
    'ready' at once instead of waiting for the transcription to end. Returns True if a
    model was (or will be) dropped."""
    wire_cuda_dlls()
    had = engine._model is not None
    engine.last_error = None
    if engine._lock.acquire(blocking=False):
        try:
            engine.unload()
        finally:
            engine._lock.release()
    else:
        engine.reload_pending = True
        logger.info("[STT] engine busy decoding -- the model is reloaded before the next transcription")
    logger.info("[STT] engine reload requested dropped=%s cuda_ready=%s", had, cuda_ready())
    return had
