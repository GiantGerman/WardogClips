"""Per-track audio ("stems") + waveform peaks for the player's track panel.

The clip / proxy mp4 carries ONE mixed audio track (game + mic + Discord). To mute, solo and
fade OBS tracks one by one like Resolve's mixer, the page plays each track as its own small
AAC file through Web Audio (the video muted) and draws its waveform from the peaks here.

- `render_window`: the stems of [t0, t1) -- the same window the clip covers -- in one read
  (it follows the clip render, so the bytes usually come from the Windows file cache).
- `outputs` / `finalize`: the proxy job appends the whole-recording stems to its own ffmpeg
  call (one read of the 10-20 GB file) and finalizes them here.

Each stem dir: t<i>.m4a per track (AAC 128k stereo; the mic normalized exactly as in the
clip mix), manifest.json {t0, t1, tracks: [{idx, name, role, in_mix, normalized, file,
peaks_min, peaks_max, rms_db, peak_db}]}. The temporary 2 kHz mono PCM used for the peaks
is deleted. Everything lives under CLIPS_DIR/stems and is pruned with the clips.
"""

import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

import numpy as np

from utils import paths

logger = logging.getLogger("wclips.stems")

# Hz of the throwaway PCM the peaks are computed from. 16 kHz keeps content up to 8 kHz (gunshots,
# sibilance): at 2 kHz the resampler's low-pass made a 3 kHz tone read 13.5 dB too quiet.
PEAK_RATE = 16000
CHUNK = 1 << 20                  # samples per chunk when reducing a long PCM (bounded memory)
BUCKETS_WINDOW = 1200            # waveform points for a clip window (~ one per pixel)
BUCKETS_FULL = 6000              # ... for a whole recording
STEM_BITRATE = "128k"
_KEY_RE = re.compile(r"^rec\d+_(?:\d+_\d+|full)_[a-z0-9_-]+$")     # never .part / .old dirs
_FILE_RE = re.compile(r"^t\d{1,2}\.m4a$")

_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


class StemError(RuntimeError):
    pass


def stems_root() -> Path:
    return paths.CLIPS_DIR / "stems"


def valid_key(key: str) -> bool:
    return bool(_KEY_RE.match(key or ""))


def valid_file(name: str) -> bool:
    return bool(_FILE_RE.match(name or ""))


def _lock(key: str) -> threading.Lock:
    with _guard:
        lk = _locks.get(key)
        if lk is None:
            lk = _locks[key] = threading.Lock()
        return lk


def n_tracks(rec: dict) -> int:
    try:
        return len(json.loads(rec.get("audio_tracks") or "[]"))
    except (TypeError, json.JSONDecodeError):
        return 0


def track_info(db, rec: dict, cfg) -> list[dict]:
    """Every audio track of the recording: OBS name, role, whether the clip mix uses it, normalized."""
    from media import audio, clips
    n = n_tracks(rec)
    roles = {r["idx"]: r["role"] for r in db.query("SELECT idx, role FROM tracks WHERE rec_id=?", (rec["id"],))}
    lay = audio.obs_layout(cfg, rec)               # a stream VOD: no OBS names / overrides
    names = lay["names"]
    plan = dict(clips.audio_plan(db, rec, cfg))
    forced = {}
    for idx, role in ((lay["game"], "game"), (lay["mic"], "mic"), (lay["voip"], "voip")):
        if 0 <= idx < n and idx not in forced:
            forced[idx] = role                     # the settings overrides, as in clips.audio_plan
    out = []
    for i in range(n):
        role = forced.get(i) or roles.get(i) or "unknown"
        if role == "unknown":
            role = audio.name_role(names[i]) if i < len(names) else None
            role = role or "unknown"
        out.append({"idx": i, "name": names[i] if i < len(names) and names[i] else f"Track {i + 1}",
                    "role": role, "in_mix": i in plan, "normalized": bool(plan.get(i))})
    return out


def key_for(rec: dict, t0: float | None, t1: float | None, info: list[dict]) -> str:
    """Cache key: recording, window (or 'full') and which tracks are normalized."""
    norm = "-".join(str(t["idx"]) for t in info if t["normalized"]) or "x"
    span = "full" if t0 is None else f"{int(round(t0 * 1000))}_{int(round(t1 * 1000))}"
    return f"rec{int(rec['id'])}_{span}_n{norm}"


def outputs(stem_dir: Path, info: list[dict]) -> list[str]:
    """ffmpeg output args: per track an AAC stem + a PEAK_RATE mono PCM for the peaks."""
    from media import audio
    args = []
    for t in info:
        i = t["idx"]
        af = audio.MIC_NORM if t["normalized"] else None
        args += ["-map", f"0:a:{i}", "-vn"]
        if af:
            args += ["-af", af]
        args += ["-c:a", "aac", "-b:a", STEM_BITRATE, "-ac", "2", "-movflags", "+faststart", "-f", "mp4",
                 str(stem_dir / f"t{i}.m4a")]
        args += ["-map", f"0:a:{i}", "-vn"]
        args += ["-af", (af + "," if af else "") + f"aresample={PEAK_RATE}"]
        args += ["-ac", "1", "-c:a", "pcm_s16le", "-f", "s16le", str(stem_dir / f"t{i}.pcm")]
    return args


def peaks(x: np.ndarray, buckets: int) -> tuple[list[float], list[float]]:
    """min / max per bucket of a float signal in [-1, 1], rounded for a compact JSON."""
    if x.size == 0:
        return [0.0] * buckets, [0.0] * buckets
    edges = np.linspace(0, x.size, buckets + 1).astype(np.int64)
    mins, maxs = [], []
    for a, b in zip(edges[:-1], edges[1:]):
        seg = x[a:max(b, a + 1)]
        mins.append(round(float(seg.min()), 3))
        maxs.append(round(float(seg.max()), 3))
    return mins, maxs


def pcm_stats(pcm: Path, buckets: int) -> tuple[list[float], list[float], float, float]:
    """(peaks_min, peaks_max, rms, peak) of an s16le PCM file, read through a memmap in chunks:
    a whole recording at 16 kHz is ~115 MB per hour per track, never loaded as floats at once."""
    size = pcm.stat().st_size // 2 if pcm.exists() else 0
    if size == 0:
        return [0.0] * buckets, [0.0] * buckets, 0.0, 0.0
    x = np.memmap(pcm, dtype="<i2", mode="r", shape=(size,))
    try:
        edges = np.linspace(0, size, buckets + 1).astype(np.int64)
        mins = np.zeros(buckets, np.float64)
        maxs = np.zeros(buckets, np.float64)
        sq = 0.0
        pk = 0
        for c0 in range(0, size, CHUNK):
            c1 = min(size, c0 + CHUNK)
            seg = np.asarray(x[c0:c1])
            sq += float(np.sum(np.square(seg, dtype=np.float64)))
            pk = max(pk, int(np.max(np.abs(seg.astype(np.int32)))))
            # buckets overlapping this chunk
            b0 = max(0, int(np.searchsorted(edges, c0, side="right")) - 1)
            b1 = min(buckets, int(np.searchsorted(edges, c1, side="left")))
            for b in range(b0, max(b1, b0 + 1)):
                a, z = max(edges[b], c0), min(max(edges[b + 1], edges[b] + 1), c1)
                if z <= a:
                    continue
                part = seg[a - c0:z - c0]
                mins[b] = min(mins[b], float(part.min()))
                maxs[b] = max(maxs[b], float(part.max()))
        rms = (sq / size) ** 0.5 / 32768.0
        return ([round(v / 32768.0, 3) for v in mins], [round(v / 32768.0, 3) for v in maxs], rms, pk / 32768.0)
    finally:
        del x


def finalize(stem_dir: Path, info: list[dict], t0: float, t1: float, buckets: int) -> dict:
    """PCM -> peaks, write manifest.json, delete the PCM. Tracks whose stem is missing are dropped."""
    tracks = []
    for t in info:
        i = t["idx"]
        pcm = stem_dir / f"t{i}.pcm"
        m4a = stem_dir / f"t{i}.m4a"
        try:
            lo, hi, rms, pk = pcm_stats(pcm, buckets)
        finally:
            try:
                pcm.unlink()
            except OSError:
                pass
        if not m4a.exists():
            continue
        tracks.append({**t, "file": m4a.name, "peaks_min": lo, "peaks_max": hi,
                       "rms_db": round(20 * np.log10(rms), 1) if rms > 0 else -120.0,
                       "peak_db": round(20 * np.log10(pk), 1) if pk > 0 else -120.0})
    man = {"t0": round(float(t0), 3), "t1": round(float(t1), 3), "buckets": buckets, "tracks": tracks,
           "utc": time.time()}
    tmp = stem_dir / "manifest.json.tmp"
    tmp.write_text(json.dumps(man), encoding="utf-8")
    tmp.replace(stem_dir / "manifest.json")
    return man


def read_manifest(key: str) -> dict | None:
    p = stems_root() / key / "manifest.json"
    if not p.is_file():
        return None
    try:
        man = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        os.utime(p)            # LRU: a used stem set is fresh
    except OSError:
        pass
    return man


def freshen(man: dict, info: list[dict]) -> dict:
    """A cached manifest with today's name / role / in_mix laid over it. The audio itself only
    depends on which tracks are normalized (part of the key); the labels and the default mute
    follow the current roles and settings (a voip_track override, a rename, a transcribe run)."""
    now = {t["idx"]: t for t in info}
    out = dict(man)
    out["tracks"] = [dict(t, **{k: now[t["idx"]][k] for k in ("name", "role", "in_mix") if t["idx"] in now})
                     for t in man.get("tracks", [])]
    return out


def install(part: Path, final: Path) -> None:
    """Move a finished .part dir to its final name. A window set is only rebuilt when its
    manifest is missing (a broken dir); on Windows a dir holding a file the page is streaming can
    be neither deleted nor renamed, so that rare case is reported instead of half-replacing it.
    Whole-recording sets never hit this: every proxy build writes a new uniquely named set."""
    if final.exists():
        shutil.rmtree(final, ignore_errors=True)
        if final.exists():
            raise StemError(f"{final.name} is in use - try again in a moment")
    try:
        part.rename(final)
    except OSError as e:
        raise StemError(f"could not install {final.name}: {e}")


def cleanup_full(rec_id: int, keep: str) -> int:
    """Remove a recording's superseded whole-recording stem sets and stale .part dirs (a set the
    page is still streaming survives this pass; the next proxy build retries). Returns dirs removed."""
    root = stems_root()
    n = 0
    if not root.is_dir():
        return 0
    for d in root.glob(f"rec{int(rec_id)}_full_*"):
        if d.name == keep or not d.is_dir():
            continue
        shutil.rmtree(d, ignore_errors=True)
        n += int(not d.exists())
    return n


def render_window(db, cfg, rec: dict, t0: float, t1: float, timeout: float = 180.0) -> tuple[str, dict]:
    """The stems of [t0, t1) (the clip's window). Cached; returns (key, manifest)."""
    from media import clips
    if not rec.get("duration_s"):
        raise StemError("probe the recording first")
    if n_tracks(rec) == 0:
        raise StemError("this recording has no audio tracks")
    dur_total = float(rec["duration_s"])
    t0 = max(0.0, min(float(t0), dur_total - 0.5))
    t1 = max(t0 + 0.5, min(float(t1), dur_total, t0 + clips.MAX_WINDOW_S))
    info = track_info(db, rec, cfg)
    key = key_for(rec, t0, t1, info)
    with _lock(key):
        man = read_manifest(key)
        if man is not None:
            return key, freshen(man, info)
        if not os.path.exists(rec["path"]):
            raise StemError("the recording is not on disk (missing)")
        d = stems_root() / key
        part = d.with_name(key + ".part")
        shutil.rmtree(part, ignore_errors=True)
        part.mkdir(parents=True, exist_ok=True)
        exe = paths.ffmpeg_exe()
        if not exe:
            raise StemError("ffmpeg not found on PATH")
        cmd = [exe, "-v", "error", "-nostdin", "-y", "-nostats", "-progress", "pipe:1",
               "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}", "-i", rec["path"], *outputs(part, info)]
        started = time.monotonic()
        try:
            clips._run(cmd, timeout)
            man = finalize(part, info, t0, t1, BUCKETS_WINDOW)
            install(part, d)
        except Exception as e:
            shutil.rmtree(part, ignore_errors=True)
            raise e if isinstance(e, StemError) else StemError(str(e))
        logger.info("[Stems] rendered rec=%s %.1f-%.1fs tracks=%d %.1fs", rec["id"], t0, t1, len(man["tracks"]),
                    time.monotonic() - started)
    return key, man


def new_full_key(db, rec: dict, cfg, info: list[dict] | None = None) -> str:
    """A fresh, unique key for one proxy build's whole-recording set (never replaced in place)."""
    return key_for(rec, None, None, info if info is not None else track_info(db, rec, cfg)) + f"_{int(time.time() * 1000)}"


def full_key(rec: dict) -> str | None:
    """The whole-recording set the last proxy build wrote (stages.proxy.stems), if any."""
    try:
        k = (json.loads(rec.get("stages") or "{}").get("proxy") or {}).get("stems")
    except (TypeError, json.JSONDecodeError):
        return None
    return k if k and valid_key(k) else None


def dir_size(d: Path) -> int:
    total = 0
    for p in d.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total
