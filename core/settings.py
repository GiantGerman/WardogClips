"""WardogClips settings -- nested dataclasses <-> settings.json (schema stamp, atomic writes, unknown keys preserved, partial merges per section).

Each section is a dataclass; its fields are the keys of that section in settings.json.
"""

import dataclasses
import json
import logging
import typing
from dataclasses import dataclass, field
from typing import Any

from utils import paths

logger = logging.getLogger("wclips.settings")

SCHEMA_VERSION = 3       # 2: proxies.codec h264_nvenc (the old default) -> libx264
                         # 3: setup + updates sections; empty personal defaults (wizard)
STT_MODELS = ("tiny.en", "small.en", "medium.en", "large-v3", "large-v3-turbo")  # turbo: ~1.6 GB download on first use
STT_COMPUTE = ("float16", "int8_float16", "int8")
OCR_DEVICES = ("auto", "gpu", "cpu")
PROXY_CODECS = ("auto", "h264_nvenc", "hevc_nvenc", "libx264")
MARKER_COLORS = ("Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia", "Rose",
                 "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream")


@dataclass
class Server:
    host: str = "127.0.0.1"
    port: int = 8558                    # env WARDOGCLIPS_PORT overrides; held by another program -> the next free one
    open_browser: bool = True
    verbose_console: bool = False


@dataclass
class Library:
    """Where the footage is. Read in place, never written to."""
    roots: list[str] = field(default_factory=list)      # the setup wizard proposes OBS's recording folder
    recursive: bool = True
    patterns: str = "*.mp4;*.mkv;*.mov"
    min_age_s: int = 120                # a file modified more recently than this is still being written by OBS
    watch_s: int = 60                   # re-scan the roots this often for new files
    auto_scan_new: bool = True


@dataclass
class Player:
    aliases: list[str] = field(default_factory=list)    # your in-game name(s), clan tag stripped (setup wizard)
    squad: list[str] = field(default_factory=list)      # squad mates (their rows are tinted in the feed)


@dataclass
class Audio:
    # OBS track names 1..6 (read from the OBS profile at startup while empty); they decide the roles
    # mic / voip (Discord) / game / desktop / music, checked against measurements per recording
    track_names: list[str] = field(default_factory=list)
    game_track: int = -1                # OBS audio track index (0-based); -1 = auto
    mic_track: int = -1                 # your microphone; -1 = auto (by name, else the track with the most speech)
    voip_track: int = -1                # the squad (Discord); -1 = auto (by name)
    voice_track: int = -1               # legacy: same as mic_track
    normalize_mic: bool = True          # lift the quiet mic (up to ~30 dB) for whisper and in clips


@dataclass
class Stt:
    enabled: bool = True
    model: str = "medium.en"
    compute: str = "float16"
    vad: bool = True
    word_timestamps: bool = True
    hotwords: str = ""                  # extra vocabulary for the decoder; player + squad names are always added
    batched: bool = False               # faster-whisper BatchedInferencePipeline (VAD speech chunks, batch_size at once)
    batch_size: int = 8                 # chunks per batch in batched mode (1-32)


@dataclass
class Detection:
    hud_profile: str = "auto"           # auto = pick by recording date
    roi_fps: int = 4                    # HUD crops per second in the full prep pass (keyframe pass = every 2 s)
    refine_window_s: float = 4.0        # full-rate decode +/- this around a candidate for the exact frame
    merge_s: float = 3.0                # signals closer than this are one event
    ocr_min_conf: float = 0.5
    ocr_fast_ticker: bool = False       # opt-in: fast OCR engine for the ticker -- ~2.7x faster, but it missed ~10 % of
                                        # the event lines on 3440x1440 footage (vision/ocr.py FAST_DET_LIMIT)
    ticker_reuse: bool = True           # reuse the last ticker reading while the crop has not changed (vision/detect.py)
    # OCR on the GPU (DirectML): auto = only while the GPU is not busy with something else (a game), checked every
    # minute (vision/gpu_guard.py); gpu = always when it works; cpu = never
    ocr_device: str = "auto"
    ticker_every: int = 1               # read the ticker on every Nth keyframe; 2 lost 8 of 14 events on one test replay (A/B): keep 1


@dataclass
class Highlights:
    keywords: str = "let's go;nice shot;got him;holy;oh my god;no way;are you kidding"
    rms_spike_db: float = 10.0          # voice level jump over the rolling floor that counts as excitement
    multikill_s: float = 10.0
    pre_roll_s: float = 8.0
    post_roll_s: float = 4.0


@dataclass
class Proxies:
    enabled: bool = True
    dir: str = ""                       # empty = <data dir>\proxies
    height: int = 536                   # 3440x1440 -> 1280x536
    bitrate_k: int = 3000
    # x264 on the CPU: OBS often records on the GPU's NVENC engine, and x264 is about as fast here because reading
    # high-bitrate footage, not encoding, is the bottleneck (and its clips came out ~40 % smaller).
    # auto = NVENC only while no other app holds an NVENC session (media/encoder.py)
    codec: str = "libx264"              # proxies
    clip_codec: str = "libx264"         # player clips


@dataclass
class Vods:
    enabled: bool = True
    dir: str = ""                       # where the provider folders go ("" = %USERPROFILE%\Videos\WardogClips VODs)
    max_height: int = 1440              # best quality up to this height (HUD OCR wants pixels; 1440 = the game's)
    parallel: int = 1                   # downloads at once (net lane threads -- restart to apply)
    add_to_library: bool = True         # put the VOD folder in the library roots on the first import


@dataclass
class Resolve:
    enabled: bool = True
    launch_on_start: bool = False       # WardogClips start: start Resolve if it is not running (off by default:
                                        # not on every launch -- e.g. while gaming)
    launch_on_send: bool = True         # a hand-off while Resolve is down starts it, waits for a project, then runs
    exe: str = ""                       # Resolve.exe ("" = the default install path)
    project: str = ""                   # open this project automatically ("" = wait for you to open one)
    wait_min: int = 15                  # a waiting hand-off gives up after this many minutes
    bin: str = "WardogClips"
    selects_name: str = "Selects {date}"
    color_kill: str = "Red"
    color_assist: str = "Yellow"
    color_death: str = "Blue"
    color_spotted: str = "Cyan"
    color_voice: str = "Green"
    color_manual: str = "Purple"


@dataclass
class Jobs:
    io_lanes: int = 1                   # probe / thumbnails
    video_lanes: int = 1                # minimum video worker threads; concurrency = drive slots + gpu_parallel
    stt_lanes: int = 1                  # CUDA compute: one at a time (one whisper model resident)
    paused: bool = False
    parallel: str = "auto"              # footage jobs per drive: "auto" = from the drive benchmark, or a fixed number
    gpu_parallel: int = 3               # video-lane jobs at once across all drives (NVDEC / NVENC sessions)
    max_parallel: int = 4               # ceiling for one drive's slots
    auto_bench: bool = True             # benchmark each library drive once, in the background, when it is idle
    auto_transcribe: bool = True        # a successful probe queues transcribe (unless already transcribed)
    auto_detect: bool = True            # ... and detect, so new footage is ready to cut


@dataclass
class Setup:
    """First-run wizard. done = False shows the wizard in the page."""
    done: bool = False
    completed_at: str = ""
    wizard_version: int = 1


@dataclass
class Updates:
    """Release check against GitHub. repo empty = never check."""
    check: bool = True
    repo: str = ""                      # "owner/WardogClips"
    last_check: str = ""
    latest: str = ""


@dataclass
class Settings:
    schema_version: int = SCHEMA_VERSION
    server: Server = field(default_factory=Server)
    library: Library = field(default_factory=Library)
    player: Player = field(default_factory=Player)
    audio: Audio = field(default_factory=Audio)
    stt: Stt = field(default_factory=Stt)
    detection: Detection = field(default_factory=Detection)
    highlights: Highlights = field(default_factory=Highlights)
    proxies: Proxies = field(default_factory=Proxies)
    resolve: Resolve = field(default_factory=Resolve)
    jobs: Jobs = field(default_factory=Jobs)
    vods: Vods = field(default_factory=Vods)
    setup: Setup = field(default_factory=Setup)
    updates: Updates = field(default_factory=Updates)
    _extra: dict[str, Any] = field(default_factory=dict, repr=False)


_SECTIONS = {"server": Server, "library": Library, "player": Player, "audio": Audio, "stt": Stt,
             "detection": Detection, "highlights": Highlights, "proxies": Proxies, "resolve": Resolve,
             "jobs": Jobs, "vods": Vods, "setup": Setup, "updates": Updates}


def _as_list(v) -> list[str]:
    """Lists arrive as JSON arrays from the API and as ';' / newline separated text from inputs."""
    if isinstance(v, str):
        parts = v.replace("\n", ";").split(";")
    elif isinstance(v, (list, tuple)):
        parts = [str(x) for x in v]
    else:
        return []
    return [p.strip() for p in parts if p and p.strip()]


def _fill(cls, data: dict) -> Any:
    fields = {f.name: f for f in dataclasses.fields(cls)}
    kw = {}
    for k, v in data.items():
        if k not in fields:
            continue
        t = fields[k].type
        try:
            if typing.get_origin(t) is list:
                v = _as_list(v)
            elif t is bool:
                v = bool(v)
            elif t is int:
                v = int(v)
            elif t is float:
                v = float(v)
            elif t is str:
                v = str(v)
        except (TypeError, ValueError):
            continue
        kw[k] = v
    return cls(**kw)


def load() -> Settings:
    if not paths.SETTINGS_FILE.exists():
        logger.info("[Settings] no settings.json -- using defaults")
        return Settings()
    try:
        raw = json.loads(paths.SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[Settings] unreadable settings.json -- using defaults", exc_info=True)
        return Settings()
    cfg = Settings(schema_version=raw.get("schema_version", SCHEMA_VERSION))
    for key, cls in _SECTIONS.items():
        if isinstance(raw.get(key), dict):
            setattr(cfg, key, _fill(cls, raw[key]))
    cfg._extra = {k: v for k, v in raw.items() if k not in _SECTIONS and k != "schema_version"}
    migrate(cfg, raw)
    validate(cfg)
    return cfg


def migrate(cfg: Settings, raw: dict | None = None) -> None:
    """Upgrade an older settings file in place (saved on the next save)."""
    if cfg.schema_version < 2:
        # every save wrote the default, so h264_nvenc here is the old default, not a choice: NVENC is the
        # one engine OBS records with (WardogClips encodes there make OBS drop frames) -> x264
        if cfg.proxies.codec == "h264_nvenc":
            cfg.proxies.codec = "libx264"
            logger.info("[Settings] migrated proxies.codec h264_nvenc -> libx264 (schema 2)")
    if cfg.schema_version < 3:
        # a settings file from before the wizard belongs to a set-up install (older versions always saved their roots):
        # never show it the first-run wizard. Its saved roots / names stay as they were.
        had = raw or {}
        if (had.get("library") or {}).get("roots") or (had.get("player") or {}).get("aliases"):
            cfg.setup.done = True
            logger.info("[Settings] migrated: existing setup -> setup.done (schema 3)")
    if cfg.schema_version < SCHEMA_VERSION:
        cfg.schema_version = SCHEMA_VERSION


def _clamp(v, lo, hi):
    return min(hi, max(lo, v))


def validate(cfg: Settings) -> None:
    s = cfg.server
    s.port = int(_clamp(s.port, 1024, 65535))
    lib = cfg.library
    lib.roots = [r.strip().rstrip("\\/") or r for r in lib.roots]
    lib.min_age_s = int(_clamp(lib.min_age_s, 0, 3600))
    lib.watch_s = int(_clamp(lib.watch_s, 10, 3600))
    if not lib.patterns.strip():
        lib.patterns = "*.mp4"
    cfg.audio.game_track = int(_clamp(cfg.audio.game_track, -1, 15))
    cfg.audio.voice_track = int(_clamp(cfg.audio.voice_track, -1, 15))
    cfg.audio.mic_track = int(_clamp(cfg.audio.mic_track, -1, 15))
    cfg.audio.voip_track = int(_clamp(cfg.audio.voip_track, -1, 15))
    cfg.audio.track_names = [str(n).strip() for n in cfg.audio.track_names][:16]
    st = cfg.stt
    if st.model not in STT_MODELS:
        st.model = "medium.en"
    if st.compute not in STT_COMPUTE:
        st.compute = "float16"
    st.batched = bool(st.batched)
    try:
        st.batch_size = int(_clamp(int(st.batch_size), 1, 32))
    except (TypeError, ValueError):
        st.batch_size = 8
    d = cfg.detection
    d.roi_fps = int(_clamp(d.roi_fps, 1, 10))
    d.refine_window_s = _clamp(float(d.refine_window_s), 1.0, 30.0)
    d.merge_s = _clamp(float(d.merge_s), 0.5, 30.0)
    d.ocr_min_conf = _clamp(float(d.ocr_min_conf), 0.0, 1.0)
    d.ocr_fast_ticker = bool(d.ocr_fast_ticker)
    d.ticker_reuse = bool(d.ticker_reuse)
    if d.ocr_device not in OCR_DEVICES:
        d.ocr_device = "auto"
    try:
        d.ticker_every = int(_clamp(int(d.ticker_every), 1, 3))
    except (TypeError, ValueError):
        d.ticker_every = 1
    h = cfg.highlights
    h.rms_spike_db = _clamp(float(h.rms_spike_db), 1.0, 40.0)
    h.multikill_s = _clamp(float(h.multikill_s), 1.0, 60.0)
    h.pre_roll_s = _clamp(float(h.pre_roll_s), 0.0, 60.0)
    h.post_roll_s = _clamp(float(h.post_roll_s), 0.0, 60.0)
    p = cfg.proxies
    p.height = int(_clamp(p.height, 240, 1440))
    p.bitrate_k = int(_clamp(p.bitrate_k, 500, 20000))
    if p.codec not in PROXY_CODECS:
        p.codec = "libx264"
    if p.clip_codec not in PROXY_CODECS:
        p.clip_codec = "libx264"
    r = cfg.resolve
    for f in ("color_kill", "color_assist", "color_death", "color_spotted", "color_voice", "color_manual"):
        if getattr(r, f) not in MARKER_COLORS:
            setattr(r, f, Resolve.__dataclass_fields__[f].default)
    r.wait_min = int(_clamp(r.wait_min, 1, 240))
    v = cfg.vods
    v.max_height = int(_clamp(v.max_height, 360, 4320))
    v.parallel = int(_clamp(v.parallel, 1, 3))
    v.dir = str(v.dir or "").strip().strip('"')
    r.exe = str(r.exe or "").strip().strip('"')
    r.project = str(r.project or "").strip()
    u = cfg.updates
    u.repo = str(u.repo or "").strip().strip("/")
    if u.repo.lower().startswith("https://github.com/"):
        u.repo = u.repo[len("https://github.com/"):].strip("/")
    j = cfg.jobs
    j.io_lanes = int(_clamp(j.io_lanes, 1, 4))
    j.video_lanes = int(_clamp(j.video_lanes, 1, 2))
    j.stt_lanes = int(_clamp(j.stt_lanes, 1, 2))
    par = str(j.parallel).strip().lower()
    if par != "auto":
        try:
            par = str(int(_clamp(int(float(par)), 1, 16)))
        except ValueError:
            par = "auto"
    j.parallel = par
    j.gpu_parallel = int(_clamp(j.gpu_parallel, 1, 8))
    j.max_parallel = int(_clamp(j.max_parallel, 1, 16))


def to_dict(cfg: Settings) -> dict:
    out: dict[str, Any] = {"schema_version": cfg.schema_version}
    for key in _SECTIONS:
        out[key] = dataclasses.asdict(getattr(cfg, key))
    return out


def apply_patch(cfg: Settings, patch: dict) -> list[str]:
    """Merge {"section": {field: value}} into cfg; returns the "section.field" names that changed."""
    changed: list[str] = []
    for sec_name, body in (patch or {}).items():
        cls = _SECTIONS.get(sec_name)
        if cls is None or not isinstance(body, dict):
            continue
        cur = getattr(cfg, sec_name)
        merged = dataclasses.asdict(cur)
        merged.update(body)
        new = _fill(cls, merged)
        for f in dataclasses.fields(cls):
            if getattr(cur, f.name) != getattr(new, f.name):
                changed.append(f"{sec_name}.{f.name}")
        setattr(cfg, sec_name, new)
    validate(cfg)
    return changed


def save(cfg: Settings) -> None:
    paths.ensure_dirs()
    out = to_dict(cfg)
    out.update(cfg._extra)
    tmp = paths.SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2), encoding="utf-8")
    tmp.replace(paths.SETTINGS_FILE)
    logger.info("[Settings] saved schema=%s", cfg.schema_version)


def patterns(cfg: Settings) -> list[str]:
    return [p.strip().lower() for p in cfg.library.patterns.split(";") if p.strip()]


def keywords(cfg: Settings) -> list[str]:
    return [k.strip().lower() for k in cfg.highlights.keywords.split(";") if k.strip()]
