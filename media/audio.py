"""The audio side of a recording: which OBS track is what, and the 16 kHz mono wavs the
transcriber reads.

OBS writes up to six stereo PCM tracks. The OBS profile names them (e.g. 1 Game,
2 Microphone, 3 Discord, 4 Desktop, 5 Browser, 6 Music -- `settings.audio.track_names`,
read from OBS at startup); a layout changes over time (older recordings may carry one mix
duplicated on every track, or a track that was routed only later), so every recording is also MEASURED on three
short windows: RMS, loud level (p99.5 of a 0.25 s envelope), Silero VAD speech share, and
duplicate tracks (identical envelopes). Roles:

- mic   -- the player (often quiet: a headset mic captured by OBS sits around ~-55 dBFS, so it is
           normalized before whisper and in the clip mix; Silero rarely sees 5 % speech on it in
           real recordings -- so a NAMED mic is used whenever it is not digital silence)
- voip  -- the squad (Discord)
- voice -- a speech track with no usable name (old layouts: a mix of everything)
- game, desktop, music, other, silent (digital silence), dup (copy of a lower track)

`settings.audio.mic_track / voip_track / game_track` (and the legacy voice_track = mic)
override the verdict.

Every ffmpeg call here is `-vn` (no video decode) and reads the footage read-only; wavs
land in the data dir's cache and are deleted by the caller.
"""

import logging
import math
import os
import subprocess
import time
import wave
from pathlib import Path

import numpy as np

from core import workmode
from utils import paths

logger = logging.getLogger("wclips.audio")

_NO_WINDOW = 0x08000000
SILENT_DB = -70.0       # mean RMS below this, with no loud level measured: digital silence (OBS writes -91 dBFS)
SILENT_LOUD_DB = -80.0  # loud level (p99.5) below this: digital silence -- a quiet mic's noise floor is ~-70
SPEECH_MIN = 0.05       # an unnamed track needs this much speech (share of the sampled time) to count as voice
DUP_CORR = 0.98         # envelopes this correlated, levels within DUP_DB: the same mix written twice
DUP_DB = 2.0
SAMPLE_RATE = 16000
SPEECH_ROLES = ("mic", "voip", "voice")          # transcribed, in this order
SPEAKER = {"mic": "me", "voip": "squad", "voice": ""}
# a dynamic normalizer for the quiet mic: lifts speech by up to ~30 dB, frame 250 ms
MIC_NORM = "dynaudnorm=f=250:g=15:p=0.9:m=30"


def obs_layout(cfg, rec: dict | None) -> dict:
    """The OBS track settings (names, overrides, mic lift) that apply to this recording. A stream VOD
    (media/vods.py: one track, game + voice + music already mixed by the streamer) has none: no OBS
    names, no overrides, no mic lift -- its single track is measured like an unnamed one."""
    if (rec or {}).get("kind") == "vod":
        return {"names": [], "game": -1, "mic": -1, "voip": -1, "voice": -1, "normalize_mic": False}
    a = cfg.audio
    return {"names": list(a.track_names or []), "game": a.game_track,
            "mic": a.mic_track if a.mic_track >= 0 else a.voice_track, "voip": a.voip_track,
            "voice": a.voice_track, "normalize_mic": bool(a.normalize_mic)}


def name_role(name: str) -> str | None:
    """OBS track name -> role (None = the name says nothing)."""
    n = (name or "").strip().lower()
    if not n or n.startswith("track") or n.isdigit():
        return None
    if "mic" in n:
        return "mic"
    if any(k in n for k in ("discord", "voip", "teamspeak", "ts3", "mumble", "comms", "squad", "party", "chat")):
        return "voip"
    if "desktop" in n:
        return "desktop"
    if any(k in n for k in ("spotify", "music", "browser", "youtube", "media")):
        return "music"
    if "game" in n:
        return "game"
    return "other"


class AudioError(RuntimeError):
    pass


def sample_windows(duration_s: float, n: int = 3, span_s: float = 30.0) -> list[tuple[float, float]]:
    """(start, length) windows at 20 / 50 / 80 % of the file: the start of a recording is
    often the lobby and the end a death screen, the middle is play."""
    if duration_s <= span_s:
        return [(0.0, max(0.1, duration_s))]
    out = []
    for f in (0.2, 0.5, 0.8)[:n]:
        start = max(0.0, min(duration_s - span_s, duration_s * f - span_s / 2))
        out.append((round(start, 3), span_s))
    return out


def extract_window(path: str, start_s: float, span_s: float, track_idxs, out_dir: Path, tag: str,
                   timeout: float = 300.0) -> dict[int, Path]:
    """One ffmpeg call, one wav per track, for a short window. Returns {idx: wav path}."""
    exe = paths.ffmpeg_exe()
    if not exe:
        raise AudioError("ffmpeg not found on PATH")
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-v", "error", "-nostdin", "-ss", f"{start_s:.3f}", "-t", f"{span_s:.3f}", "-i", path, "-vn"]
    outs = {}
    for i in track_idxs:
        out = out_dir / f"{tag}_t{i}_{int(start_s)}.wav"
        cmd += ["-map", f"0:a:{i}", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", "-y", str(out)]
        outs[i] = out
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=workmode.child_flags(_NO_WINDOW))
    except subprocess.TimeoutExpired:
        raise AudioError(f"ffmpeg timed out extracting a {span_s:.0f}s window")
    if r.returncode != 0:
        raise AudioError((r.stderr or "ffmpeg failed").strip()[-300:])
    return outs


def read_wav(path: Path) -> np.ndarray:
    """16-bit mono wav -> float32 in [-1, 1]."""
    with wave.open(str(path), "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
        ch = w.getnchannels()
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x


def rms_db(x: np.ndarray) -> float:
    if x.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    return 20.0 * math.log10(rms) if rms > 0 else -120.0


def speech_share(x: np.ndarray, sr: int = SAMPLE_RATE) -> float:
    """Fraction of the clip Silero VAD calls speech (faster-whisper's bundled VAD)."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    if x.size < sr // 2:
        return 0.0
    opts = VadOptions(min_silence_duration_ms=500, speech_pad_ms=100)
    spans = get_speech_timestamps(x, vad_options=opts, sampling_rate=sr)
    voiced = sum(max(0, s["end"] - s["start"]) for s in spans)
    return min(1.0, voiced / float(x.size))


def _envelope(x: np.ndarray, win: int = SAMPLE_RATE // 4) -> np.ndarray:
    m = x.size // win
    if m == 0:
        return np.zeros(0)
    e = np.sqrt(np.mean(np.square(x[: m * win].reshape(m, win), dtype=np.float64), axis=1))
    return 20.0 * np.log10(np.maximum(e, 1e-6))


def measure_tracks(path: str, duration_s: float, n_tracks: int, cache_dir: Path, tag: str,
                   vad=speech_share) -> dict[int, dict]:
    """{idx: {"rms_db", "loud_db", "speech_share", "dup_of"?}} over the sample windows. loud_db is
    the p99.5 of a 0.25 s envelope (a quiet mic that talks has a low mean but a real loud level);
    dup_of marks a track whose envelope is a copy of a lower-numbered one."""
    if n_tracks <= 0:
        return {}
    acc = {i: {"rms": [], "speech": [], "env": []} for i in range(n_tracks)}
    t0 = time.monotonic()
    for start, span in sample_windows(duration_s):
        wavs = extract_window(path, start, span, range(n_tracks), cache_dir, tag)
        try:
            for i, p in wavs.items():
                x = read_wav(p)
                db = rms_db(x)
                env = _envelope(x)
                acc[i]["rms"].append(db)
                acc[i]["env"].append(env)
                loud = float(np.percentile(env, 99.5)) if env.size else db
                acc[i]["speech"].append(0.0 if loud < SILENT_LOUD_DB else float(vad(x)))
        finally:
            for p in wavs.values():
                try:
                    os.remove(p)
                except OSError:
                    pass
    out: dict[int, dict] = {}
    envs: dict[int, np.ndarray] = {}
    for i, a in acc.items():
        env = np.concatenate(a["env"]) if a["env"] else np.zeros(0)
        envs[i] = env
        out[i] = {"rms_db": round(float(np.mean(a["rms"])), 1),
                  "loud_db": round(float(np.percentile(env, 99.5)), 1) if env.size else round(float(np.mean(a["rms"])), 1),
                  "speech_share": round(float(np.mean(a["speech"])), 3)}
    # duplicates: some OBS layouts write one mix to every track
    for j in range(n_tracks):
        for i in range(j):
            if "dup_of" in out[i] or out[i]["loud_db"] < SILENT_LOUD_DB or out[j]["loud_db"] < SILENT_LOUD_DB:
                continue
            a, b = envs[i], envs[j]
            if a.size < 8 or a.size != b.size or abs(out[i]["rms_db"] - out[j]["rms_db"]) > DUP_DB:
                continue
            if np.std(a) < 1e-6 or np.std(b) < 1e-6:
                continue
            if float(np.corrcoef(a, b)[0, 1]) >= DUP_CORR:
                out[j]["dup_of"] = i
                break
    logger.info("[Audio] measured tracks=%d %.1fs %s", n_tracks, time.monotonic() - t0,
                " ".join(f"t{i}:{m['rms_db']:.0f}/{m['loud_db']:.0f}dB/{m['speech_share']:.0%}"
                         + (f"=t{m['dup_of']}" if "dup_of" in m else "") for i, m in out.items()))
    return out


def _is_silent(m: dict) -> bool:
    if "loud_db" in m:
        return m["loud_db"] < SILENT_LOUD_DB
    return m["rms_db"] < SILENT_DB


def decide_roles(measures: dict[int, dict], voice_override: int = -1, game_override: int = -1,
                 names: list[str] | None = None, mic_override: int = -1, voip_override: int = -1) -> dict[int, str]:
    """Roles per track (see the module doc). With OBS track names, a named mic / Discord track is
    used whenever it is not silent or a duplicate. Without names (or when the names find no
    speech track, i.e. an older layout) the track with the most speech becomes `voice`, the
    loudest of the rest `game`. voice_override is the legacy name of mic_override."""
    if not measures:
        return {}
    if mic_override < 0:
        mic_override = voice_override
    forced = {}
    for idx, role in ((game_override, "game"), (mic_override, "mic"), (voip_override, "voip")):
        if idx in measures and idx not in forced:
            forced[idx] = role
    # an explicit override wins over the measurement
    silent = {i for i, m in measures.items() if _is_silent(m) and i not in forced}
    dup = {i for i, m in measures.items() if "dup_of" in m and i not in silent and i not in forced}
    live = [i for i in sorted(measures) if i not in silent and i not in dup]
    roles: dict[int, str] = {i: ("silent" if i in silent else "dup") for i in measures if i not in live}
    named = {}
    for i in live:
        r = name_role(names[i]) if names and i < len(names) else None
        if r:
            named[i] = r
    for i in live:
        roles[i] = forced.get(i) or named.get(i) or "other"
    have_speech_track = any(r in ("mic", "voip") for r in roles.values())
    if not have_speech_track:
        # unnamed (or a layout from before the names applied): most speech = voice, loudest of the rest = game
        cands = [(measures[i]["speech_share"], -i, i) for i in live if i not in forced]
        if cands:
            share, _, i = max(cands)
            if share >= SPEECH_MIN:
                roles[i] = "voice"
    if "game" not in roles.values():
        # no track named or forced as the game: the loudest unclaimed one
        cands = [(measures[i]["rms_db"], -i, i) for i in live if roles[i] == "other" and i not in forced]
        if cands:
            roles[max(cands)[2]] = "game"
    return {i: roles[i] for i in sorted(roles)}


def speech_tracks(roles: dict[int, str]) -> list[tuple[int, str]]:
    """[(idx, role)] to transcribe: mic first, then the squad, then unnamed voice tracks."""
    order = {r: k for k, r in enumerate(SPEECH_ROLES)}
    return sorted(((i, r) for i, r in roles.items() if r in order), key=lambda x: (order[x[1]], x[0]))


def extract_voice(path: str, idx: int, out: Path, duration_s: float, on_progress=None,
                  timeout: float = 3600.0) -> Path:
    """Whole voice track -> 16 kHz mono wav, with progress parsed from ffmpeg's -progress stream."""
    exe = paths.ffmpeg_exe()
    if not exe:
        raise AudioError("ffmpeg not found on PATH")
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-v", "error", "-nostdin", "-nostats", "-progress", "pipe:1", "-i", path, "-vn",
           "-map", f"0:a:{idx}", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", "-y", str(out)]
    t0 = time.monotonic()
    proc = workmode.track(subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                           errors="replace", creationflags=workmode.child_flags(_NO_WINDOW)))
    try:
        for line in proc.stdout:
            if time.monotonic() - t0 > timeout:
                proc.kill()
                raise AudioError("ffmpeg timed out extracting the voice track")
            if on_progress and duration_s and line.startswith("out_time_us="):
                try:
                    on_progress(min(1.0, int(line.split("=", 1)[1]) / 1e6 / duration_s))
                except ValueError:
                    pass
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    if proc.returncode != 0:
        err = (proc.stderr.read() if proc.stderr else "") or "ffmpeg failed"
        raise AudioError(err.strip()[-300:])
    logger.info("[Audio] voice track %d extracted %.1fs -> %s", idx, time.monotonic() - t0, out.name)
    return out


def extract_tracks(path: str, specs: list[tuple[int, Path, bool]], duration_s: float, on_progress=None,
                   timeout: float = 3600.0) -> list[Path]:
    """Whole tracks -> 16 kHz mono wavs in ONE read of the file (footage often lives on a slow HDD).
    specs = [(track idx, out wav, normalize)]; normalize runs MIC_NORM (the quiet mic). A track may
    appear twice (normalized for whisper, raw for the excitement envelope)."""
    exe = paths.ffmpeg_exe()
    if not exe:
        raise AudioError("ffmpeg not found on PATH")
    if not specs:
        return []
    cmd = [exe, "-v", "error", "-nostdin", "-nostats", "-progress", "pipe:1", "-i", path, "-vn"]
    for idx, out, norm in specs:
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd += ["-map", f"0:a:{idx}", "-ac", "1", "-ar", str(SAMPLE_RATE)]
        if norm:
            cmd += ["-af", MIC_NORM]
        cmd += ["-c:a", "pcm_s16le", "-y", str(out)]
    t0 = time.monotonic()
    proc = workmode.track(subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                           errors="replace", creationflags=workmode.child_flags(_NO_WINDOW)))
    try:
        for line in proc.stdout:
            if time.monotonic() - t0 > timeout:
                proc.kill()
                raise AudioError("ffmpeg timed out extracting the voice tracks")
            if on_progress and duration_s and line.startswith("out_time_us="):
                try:
                    on_progress(min(1.0, int(line.split("=", 1)[1]) / 1e6 / duration_s))
                except ValueError:
                    pass
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    if proc.returncode != 0:
        err = (proc.stderr.read() if proc.stderr else "") or "ffmpeg failed"
        raise AudioError(err.strip()[-300:])
    logger.info("[Audio] tracks %s extracted %.1fs (normalized: %s)", ",".join(str(i + 1) for i, _o, _n in specs),
                time.monotonic() - t0, ",".join(str(i + 1) for i, _o, n in specs if n) or "-")
    return [o for _i, o, _n in specs]
