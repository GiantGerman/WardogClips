"""Stream VOD import: a link to a past stream (or video) on YouTube, Twitch, Kick, TikTok or Rumble
is downloaded with yt-dlp and then goes through the library like an OBS recording (probe ->
transcribe -> detect, clips, Resolve).

- The provider comes from the link's host; each has its own folder under the VOD root
  (Settings > VODs > dir; default <the Videos known folder>\\WardogClips VODs), made on first use:
  ``<root>\\Twitch\\VOD_Twitch_2026-11-05_20-14-03_2612345678.mp4``. The name carries the stream's
  START (local time, like an OBS recording's name), so the library dates it and orders it.
- A link to a stream that is live right now, upcoming, still processing, or to a whole channel /
  playlist is refused with a hint -- this imports finished VODs only.
- yt-dlp runs as a child process (killable, its progress parsed from --newline lines) and writes
  into ``<provider dir>\\_downloading\\`` -- the library walk skips folders starting with '_' -- and
  the finished mp4 is moved next to it only when complete, with a ``.vod.json`` sidecar (url, title,
  channel, start) the library reads into recordings.title / source_url.
- Quality: the best up to Settings > VODs > max_height (1440 by default: the HUD OCR wants pixels),
  H.264 preferred at equal size (Resolve, NVDEC), merged / remuxed to mp4 with our ffmpeg.

Nothing here touches the OBS footage, and a download never runs on the GPU.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from core import workmode
from utils import paths
from utils.i18n import msg as _msg

logger = logging.getLogger("wclips.vods")

PROVIDERS = (
    ("YouTube", ("youtube.com", "youtu.be", "youtube-nocookie.com")),
    ("Twitch", ("twitch.tv",)),
    ("Kick", ("kick.com",)),
    ("TikTok", ("tiktok.com",)),
    ("Rumble", ("rumble.com",)),
)
PROVIDER_NAMES = tuple(p for p, _ in PROVIDERS)
TMP_DIR = "_downloading"                 # skipped by the library walk ('_' prefix)
SIDECAR = ".vod.json"
_NO_WINDOW = 0x08000000
_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")
LIVE_STATES = {"is_live": "is live right now", "is_upcoming": "has not started yet",
               "post_live": "just ended and is still being processed by the site"}


class VodError(RuntimeError):
    """A link that can't be imported, or a download that failed; the message is for the page."""


# ---- links / folders / names ---------------------------------------------------------------------
def provider_of(url: str) -> str | None:
    try:
        host = (urlparse(url.strip()).hostname or "").lower()
    except ValueError:
        return None
    for name, hosts in PROVIDERS:
        if any(host == h or host.endswith("." + h) for h in hosts):
            return name
    return None


NOT_SUPPORTED = _msg("server.vods.not_supported",
                     "not a supported site -- YouTube, Twitch, Kick, TikTok and Rumble links work")["message"]
NO_YTDLP = _msg("server.vods.no_ytdlp", "yt-dlp is not installed (reinstall WardogClips)")["message"]


def check_url(url: str) -> tuple[str, str]:
    """(clean url, provider) or VodError."""
    u = (url or "").strip()
    if not re.match(r"^https?://", u, re.I):
        if re.match(r"^(www\.|m\.)?[a-z0-9.-]+\.[a-z]{2,}/", u, re.I):
            u = "https://" + u
        else:
            raise VodError(_msg("server.vods.full_link", "paste the full link of the VOD (https://...)")["message"])
    prov = provider_of(u)
    if prov is None:
        raise VodError(NOT_SUPPORTED)
    return u, prov


def _known_videos_dir() -> Path | None:
    """The Videos known folder (FOLDERID_Videos) -- where Explorer's Videos really is: moved to another drive (common
    with a small C:) or redirected by policy. None off Windows or when the shell does not answer."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]
        u = uuid.UUID("{18989B1D-99B5-455B-841C-AB7C74E4DDFC}")
        g = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        out = ctypes.c_wchar_p()
        shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32
        hr = shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out))
        try:
            return Path(out.value) if hr == 0 and out.value else None
        finally:
            ole32.CoTaskMemFree(out)
    except Exception:  # noqa: BLE001 - any trouble -> the plain %USERPROFILE%\Videos
        return None


def default_vod_root() -> Path:
    """Where VODs go when Settings > VODs > dir is empty: the Videos known folder (it may be moved to another drive
    or redirected), but never a OneDrive-synced one -- a multi-GB VOD must not be uploaded silently -> then %USERPROFILE%\\Videos."""
    home = os.environ.get("USERPROFILE")
    plain = (Path(home) if home else Path.home()) / "Videos"
    kf = _known_videos_dir()
    if kf is not None and not any(part.lower().startswith("onedrive") for part in kf.parts):
        base = kf
    else:
        base = plain
    return base / "WardogClips VODs"


def vod_root(cfg) -> Path:
    if cfg.vods.dir:
        return Path(cfg.vods.dir)
    return default_vod_root()


def provider_dir(cfg, provider: str, create: bool = True) -> Path:
    d = vod_root(cfg) / provider
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def start_utc(info: dict) -> float | None:
    """When the stream started: release_timestamp (YouTube: the live start), else timestamp, else the
    upload date at midnight."""
    for k in ("release_timestamp", "timestamp"):
        v = info.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    d = info.get("release_date") or info.get("upload_date")
    if d and re.fullmatch(r"\d{8}", str(d)):
        return datetime.strptime(str(d), "%Y%m%d").timestamp()
    return None


def file_stem(provider: str, ts: float | None, vid: str) -> str:
    """VOD_<Provider>_<YYYY-MM-DD_HH-MM-SS local start>_<id> -- core.library.parse_name reads it."""
    when = datetime.fromtimestamp(ts if ts else time.time()).strftime("%Y-%m-%d_%H-%M-%S")
    clean = _ID_RE.sub("", str(vid or ""))[:40] or "vod"
    return f"VOD_{provider}_{when}_{clean}"


CHANNEL_URLS = {                          # a streamer's page (also where the VOD browser lists their VODs from)
    "YouTube": "https://www.youtube.com/{key}",
    "Twitch": "https://www.twitch.tv/{key}",
    "Kick": "https://kick.com/{key}",
    "TikTok": "https://www.tiktok.com/{key}",
    "Rumble": "https://rumble.com/{key}",
}


def canon_channel_key(key) -> str:
    """A streamer key as stored everywhere (recordings, zone templates, favorites): lower case, except a YouTube
    'channel/<id>' -- channel ids are case-sensitive (a lower-cased id answers HTTP 400)."""
    k = str(key or "").strip().strip("/")
    if k.lower().startswith("channel/"):
        return "channel/" + k.split("/", 1)[1]
    return k.lower()


def channel_identity(provider: str, info: dict) -> dict:
    """The streamer behind a VOD as {key, name, url}: key = the stable handle per site, lower case (a YouTube 'channel/<id>' keeps its case) --
    YouTube '@examplestreamer', Twitch 'examplestreamer', Kick 'examplestreamer', TikTok '@examplestreamer', Rumble
    'c/examplestreamer' (from yt-dlp's fields). Zone templates and favorites are keyed on (provider, key)."""
    g = lambda k: (str(info.get(k)).strip() if info.get(k) not in (None, "", "NA") else "")   # noqa: E731
    key = ""
    if provider == "YouTube":
        key = g("uploader_id") if g("uploader_id").startswith("@") else ""
        if not key:
            m = re.search(r"youtube\.com/(@[^/?#]+)", g("uploader_url") or g("channel_url"))
            key = m.group(1) if m else ""
        if not key and g("channel_id"):
            key = "channel/" + g("channel_id")
    elif provider in ("Twitch", "Kick"):
        key = g("uploader_id") or (g("uploader") or g("channel")).replace(" ", "")    # the login / slug
    elif provider == "TikTok":
        m = re.search(r"tiktok\.com/(@[^/?#]+)", g("uploader_url"))
        key = m.group(1) if m else ("@" + g("uploader") if g("uploader") else "")
    elif provider == "Rumble":
        m = re.search(r"rumble\.com/((?:c|user)/[^/?#]+)", g("channel_url") or g("uploader_url"))
        key = m.group(1) if m else ""
    key = canon_channel_key(key)
    name = g("channel") or g("uploader") or key
    return {"key": key, "name": name, "url": CHANNEL_URLS[provider].format(key=key) if key else ""}


def read_sidecar(mp4: Path) -> dict:
    try:
        return json.loads(mp4.with_suffix(SIDECAR).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def existing(cfg, provider: str, vid: str) -> Path | None:
    """An already imported VOD with this id (whatever its date), so a second import is a no-op."""
    d = provider_dir(cfg, provider, create=False)
    if not d.is_dir() or not vid:
        return None
    clean = _ID_RE.sub("", str(vid))[:40]
    for p in d.glob(f"VOD_{provider}_*_{clean}.mp4"):
        return p
    return None


# ---- yt-dlp ----------------------------------------------------------------------------------------
def available() -> bool:
    try:
        import yt_dlp  # noqa: F401
        return True
    except ImportError:
        return False


# sites behind Cloudflare: yt-dlp must present a real browser's TLS fingerprint (curl_cffi, the
# yt-dlp[curl-cffi] extra). Without it Rumble answers 403 and Kick warns and may refuse.
IMPERSONATE = {"Rumble": "chrome", "Kick": "chrome"}


def impersonation_available() -> bool:
    try:
        import curl_cffi  # noqa: F401
        return True
    except ImportError:
        return False


def ytdlp_pack_dir() -> Path | None:
    """packs/ytdlp (a newer yt-dlp downloaded in Settings > Add-ons) once it is complete AND newer than the bundled
    yt-dlp (core.packs.ytdlp_path: after an app update bundles a newer one, an old pack must not win)."""
    try:
        from core import packs
        return packs.ytdlp_path()
    except Exception:  # noqa: BLE001 - packs unavailable: the bare manifest check
        d = paths.PACKS_DIR / "ytdlp"
        return d if (d / "PACK.json").is_file() and (d / "yt_dlp").is_dir() else None


# The installed runtime is an embeddable Python whose ._pth file puts it in isolated mode: PYTHONPATH is IGNORED
# there, so an add-on passed that way would install, report ready and change nothing. The child
# therefore puts the add-on first on sys.path itself and runs yt_dlp as `python -m yt_dlp` would. -s keeps a user's
# `pip install --user` packages (user site-packages) out of the child.
YTDLP_BOOT = ("import sys, runpy; sys.path.insert(0, sys.argv.pop(1)); "
              "runpy.run_module('yt_dlp', run_name='__main__', alter_sys=True)")


def ytdlp_cmd() -> list[str]:
    """The start of every yt-dlp child command line (the yt-dlp options follow)."""
    # paths.python_exe(): under pythonw.exe (the tray app) the child needs the console python.exe
    pack = ytdlp_pack_dir()
    if pack is not None:
        return [paths.python_exe(), "-s", "-c", YTDLP_BOOT, str(pack)]
    return [paths.python_exe(), "-s", "-m", "yt_dlp"]


def child_env() -> dict:
    """The environment of every yt-dlp child: UTF-8 I/O (paths / errors with any character). The add-on reaches the
    child through ytdlp_cmd() (not PYTHONPATH, which the installed runtime ignores)."""
    return dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")


def js_runtime() -> tuple[str, list[str]]:
    """The JavaScript runtime yt-dlp uses for YouTube's format signatures -> (name, extra args). Node on PATH, else the
    deno pack, else deno on PATH, else none -- YouTube then fails and the error carries
    YOUTUBE_HINT."""
    if shutil.which("node"):
        return "node", ["--js-runtimes", "node"]
    deno = paths.PACKS_DIR / "deno" / "deno.exe"
    if deno.is_file():
        return "deno-pack", ["--js-runtimes", f"deno:{deno}"]
    if shutil.which("deno"):
        return "deno", ["--js-runtimes", "deno"]
    return "none", []


YOUTUBE_HINT = "install the YouTube add-on in Settings > Add-ons"


def _base_cmd(provider: str | None = None) -> list[str]:
    cmd = ytdlp_cmd() + ["--ignore-config", "--no-playlist", "--no-warnings"]
    if provider in IMPERSONATE and impersonation_available():
        cmd += ["--impersonate", IMPERSONATE[provider]]
    ff = paths.ffmpeg_exe()
    if ff:
        cmd += ["--ffmpeg-location", str(Path(ff).parent)]
    # YouTube needs a JavaScript runtime for its format signatures (js_runtime: node / deno pack / deno / none)
    cmd += js_runtime()[1]
    return cmd


def format_args(cfg) -> list[str]:
    h = int(cfg.vods.max_height)
    # the fix-up / merge ffmpeg look further into the input: Kick's HLS starts without the H.264 frame size in
    # ffmpeg's default probe window -> "Could not write header (incorrect codec parameters ?)"
    probe = "ffmpeg_i:-analyzeduration 100M -probesize 100M"
    return ["-f", "bv*+ba/b", "-S", f"res:{h},fps,vcodec:h264,acodec:aac",
            "--merge-output-format", "mp4", "--remux-video", "mp4",
            "--ppa", f"FixupM3u8+{probe}", "--ppa", f"Merger+{probe}", "--ppa", f"VideoRemuxer+{probe}"]


def _run_json(cmd: list[str], timeout: float, provider: str | None = None) -> dict:
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=timeout, env=child_env(), creationflags=_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        raise VodError(_msg("server.vods.site_timeout", "the site did not answer within {seconds} s", seconds=f"{timeout:.0f}")["message"])
    if cp.returncode != 0:
        raise VodError(_ytdlp_error(cp.stderr or cp.stdout, provider))
    try:
        return json.loads(cp.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise VodError("yt-dlp answered something that is not a video description")


def _ytdlp_error(text: str, provider: str | None = None) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    err = next((ln for ln in reversed(lines) if ln.startswith("ERROR")), lines[-1] if lines else "yt-dlp failed")
    err = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*)?", "", err)
    low = err.lower()
    if "not currently live" in low or "is offline" in low:
        err += " -- that is the channel's live link; open the past broadcast (e.g. twitch.tv/videos/<id>) and paste its link"
    elif "sign in" in low or "login" in low or "subscriber" in low or "members" in low:
        err += " -- this VOD needs an account (subscriber / members-only / age-gated); WardogClips downloads public VODs"
    if provider == "YouTube" and js_runtime()[0] == "none":
        # without a JS runtime YouTube's signatures cannot be solved: the hint must survive the length cap
        return err[:300 - len(YOUTUBE_HINT) - 4] + " -- " + YOUTUBE_HINT
    return err[:300]


def inspect(cfg, url: str, timeout: float = 90.0) -> dict:
    """What the link is, without downloading: {provider, id, title, channel, start_utc, duration_s,
    height, fps, vcodec, size_bytes, url}. Live / upcoming / channel links raise VodError."""
    url, provider = check_url(url)
    if not available():
        raise VodError(NO_YTDLP)
    # --flat-playlist: a channel / playlist link answers at once with its listing (refused below) instead of
    # yt-dlp resolving every video in it (a whole channel took > 90 s)
    if provider in IMPERSONATE and not impersonation_available():
        raise VodError(f"{provider} needs browser impersonation: install it with "
                       "venv\\Scripts\\python.exe -m pip install \"yt-dlp[default,curl-cffi]\"")
    info = _run_json(_base_cmd(provider) + format_args(cfg) + ["--flat-playlist", "-J", "--", url], timeout, provider)
    if info.get("_type") in ("playlist", "multi_video") or info.get("entries") is not None:
        raise VodError(_msg("server.vods.channel_link", "that is a channel / playlist link -- open one VOD and paste its link")["message"])
    status = info.get("live_status")
    if info.get("is_live") or status in LIVE_STATES:
        raise VodError(f"that stream {LIVE_STATES.get(status, 'is live right now')} -- paste the VOD link once "
                       "the stream has ended and the site has the replay")
    fmts = info.get("requested_formats") or [info]
    size = sum(int(f.get("filesize") or f.get("filesize_approx") or 0) for f in fmts) or None
    v = next((f for f in fmts if f.get("vcodec") not in (None, "none")), info)
    ch = channel_identity(provider, info)
    return {"provider": provider, "url": info.get("webpage_url") or url, "id": str(info.get("id") or ""),
            "channel_key": ch["key"], "channel_url": ch["url"],
            "title": info.get("title") or info.get("fulltitle") or "", "channel": info.get("channel") or info.get("uploader") or "",
            "start_utc": start_utc(info), "duration_s": info.get("duration"), "was_live": bool(info.get("was_live")),
            "height": v.get("height") or info.get("height"), "fps": v.get("fps") or info.get("fps"),
            "vcodec": (v.get("vcodec") or "").split(".")[0], "size_bytes": size, "parts": max(1, len(fmts)),
            "extractor": info.get("extractor_key") or info.get("extractor")}


_PROG = re.compile(r"^WCLIPS\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)")


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _kill_tree(proc) -> None:
    """yt-dlp runs ffmpeg (merge / HLS fix-up) as its own child: end the whole tree, never just yt-dlp.
    Only our own child process and its descendants -- nothing is looked up by name."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=15,
                           creationflags=_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _probe_streams(path: Path) -> dict:
    exe = paths.ffprobe_exe() or "ffprobe"
    cp = subprocess.run([exe, "-v", "error", "-analyzeduration", "100M", "-probesize", "100M", "-show_entries",
                         "format=format_name:stream=codec_type,width,height", "-of", "json", str(path)],
                        capture_output=True, text=True, timeout=120, creationflags=_NO_WINDOW if os.name == "nt" else 0)
    try:
        return json.loads(cp.stdout or "{}")
    except ValueError:
        return {}


def ensure_mp4(path: Path) -> str:
    """The downloaded file must be a real MP4 with a known frame size -- the library's ffprobe, NVDEC and
    Resolve all need that. HLS VODs arrive as MPEG-TS that yt-dlp's fix-up remuxes; if that was skipped or
    missed (Kick: no frame size in ffmpeg's default probe window), remux here with a deep probe.
    Returns 'ok' or 'remuxed'; VodError when the file has no usable video."""
    info = _probe_streams(path)
    fmt = (info.get("format") or {}).get("format_name") or ""
    vids = [s for s in info.get("streams") or [] if s.get("codec_type") == "video"]
    clean = vids and all(int(s.get("width") or 0) > 0 for s in vids)
    if clean and "mp4" in fmt:
        # the library's plain ffprobe (default probe window) must see the frame size too
        cp = subprocess.run([paths.ffprobe_exe() or "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "stream=width", "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=60,
                            creationflags=_NO_WINDOW if os.name == "nt" else 0)
        if (cp.stdout or "").strip().isdigit() and int(cp.stdout.strip()) > 0:
            return "ok"
    if not vids:
        raise VodError("the download has no video stream")
    tmp = path.with_name(path.stem + ".remux.mp4")
    ff = paths.ffmpeg_exe() or "ffmpeg"
    cmd = [ff, "-v", "error", "-y", "-analyzeduration", "100M", "-probesize", "100M", "-i", str(path),
           "-map", "0:v:0", "-map", "0:a?", "-dn", "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(tmp)]
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=3 * 3600,
                        creationflags=workmode.child_flags(_NO_WINDOW if os.name == "nt" else 0))
    if cp.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 1024:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise VodError("the download could not be turned into an MP4: " + ((cp.stderr or "").strip().splitlines() or ["?"])[-1][:200])
    tmp.replace(path)
    logger.info("[VODs] remuxed to a clean MP4 (format was %s) %s", fmt or "?", path.name)
    return "remuxed"


def space_needed(size_bytes: float, have_bytes: float = 0.0) -> float:
    """Bytes a download needs free: the merge (YouTube: the merged file while both parts exist) and the
    HLS fix-up (Twitch / Kick: a full ffmpeg copy next to the .mp4) both peak at ~2x the VOD."""
    return max(0.0, size_bytes * 2.1 - have_bytes) + 2e9


def download(cfg, meta: dict, on_progress=None, stop=None, timeout: float = 12 * 3600,
             reserved_bytes: float = 0.0) -> Path:
    """Download one VOD (meta from inspect()) into its provider folder; returns the final mp4.
    reserved_bytes = what other running downloads on the same drive will still write."""
    import queue as _queue
    import threading as _threading
    provider = meta["provider"]
    pdir = provider_dir(cfg, provider)
    stem = file_stem(provider, meta.get("start_utc"), meta.get("id"))
    final = pdir / f"{stem}.mp4"
    if final.exists():
        return final
    tmp = pdir / TMP_DIR
    tmp.mkdir(exist_ok=True)
    if meta.get("size_bytes"):
        have = sum(p.stat().st_size for p in tmp.glob(f"{stem}.*") if p.is_file())     # a resumed download
        need = space_needed(float(meta["size_bytes"]), have) + reserved_bytes
        free = shutil.disk_usage(pdir).free
        if free < need:
            raise VodError(_msg("server.vods.no_space", "not enough space in {dir}: {free} GB free, about {need} GB needed "
                                "(the VOD is ~{size} GB; joining video + audio briefly needs it twice)",
                                dir=str(pdir), free=f"{free / 1e9:.0f}", need=f"{need / 1e9:.0f}",
                                size=f"{meta['size_bytes'] / 1e9:.1f}")["message"])
    cmd = _base_cmd(provider) + format_args(cfg) + [
        # --progress: yt-dlp prints no progress when its output is not a terminal (ours is a pipe); --print
        # implies --quiet, so the merge / fix-up phase reports through its own postprocess template
        "--progress", "--newline", "--no-mtime", "--concurrent-fragments", "4", "--retries", "10", "--fragment-retries", "10",
        "-o", str(tmp / f"{stem}.%(ext)s"),
        "--progress-template", "download:WCLIPS %(progress.downloaded_bytes)s %(progress.total_bytes)s "
                               "%(progress.total_bytes_estimate)s %(progress.speed)s %(progress.eta)s "
                               "%(progress.fragment_index)s %(progress.fragment_count)s",
        "--progress-template", "postprocess:WCLIPSPP %(progress.postprocessor)s %(progress.status)s",
        "--print", "after_move:WCLIPSFILE %(filepath)s", "--", meta["url"]]
    logger.info("[VODs] download provider=%s id=%s -> %s", provider, meta.get("id"), final)
    env = child_env()          # UTF-8 (paths / errors with any character); the ytdlp pack rides in the cmd
    # the work mode's priority (core/workmode.py); yt-dlp's own ffmpeg children inherit it
    proc = workmode.track(subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                           errors="replace", env=env,
                                           creationflags=workmode.child_flags(_NO_WINDOW if os.name == "nt" else 0)))
    # a reader thread: yt-dlp is silent for minutes while ffmpeg merges / fixes up a big VOD, and Stop and
    # the timeout must still be noticed then
    lines: _queue.Queue = _queue.Queue()

    def _read():
        try:
            for ln in proc.stdout:
                lines.put(ln)
        finally:
            lines.put(None)
    reader = _threading.Thread(target=_read, name="yt-dlp-out", daemon=True)
    reader.start()
    t0, got, tail = time.monotonic(), None, []
    parts, part, last = max(1, int(meta.get("parts") or 1)), 0, 0.0   # video + audio = two files, one bar
    try:
        while True:
            if stop is not None and stop():
                _kill_tree(proc)
                raise VodError("stopped")
            if time.monotonic() - t0 > timeout:
                _kill_tree(proc)
                raise VodError(f"download took longer than {timeout / 3600:.0f} h")
            try:
                line = lines.get(timeout=1.0)
            except _queue.Empty:
                continue
            if line is None:
                break
            line = line.rstrip()
            if line.startswith("WCLIPSPP "):
                bits = line.split()
                if len(bits) >= 3 and bits[2] == "started" and on_progress:
                    pp = bits[1]
                    on_progress(0.98, _msg("server.vods.joining", "joining video + audio")["message"] if "Merger" in pp else
                                _msg("server.vods.fixing", "fixing up the stream file")["message"] if "Fixup" in pp
                                else f"finishing ({pp})")
                continue
            m = _PROG.match(line)
            if m:
                done, total, est, speed, eta, fi, fc = (_num(x) for x in m.groups())
                tot = total or est
                frac = (done / tot) if (done and tot) else ((fi / fc) if (fi and fc) else None)
                if frac is not None:
                    if frac + 0.3 < last and part < parts - 1:
                        part += 1                      # the next file (the audio) started from zero
                    last = frac
                    if on_progress:
                        on_progress(min(0.97, (part + frac) / parts),
                                    (f"part {part + 1}/{parts}: " if parts > 1 else "")
                                    + f"{(done or 0) / 1e6:.0f} MB" + (f" of {tot / 1e6:.0f} MB" if tot else "")
                                    + (f", {speed / 1e6:.1f} MB/s" if speed else "")
                                    + (f", {int(eta) // 60}:{int(eta) % 60:02d} left" if eta else ""))
                continue
            if line.startswith("WCLIPSFILE "):
                got = Path(line[len("WCLIPSFILE "):].strip())
                continue
            tail.append(line)
            del tail[:-15]
        rc = proc.wait(timeout=60)
    finally:
        _kill_tree(proc)
    if got is None or not got.exists():
        got = tmp / f"{stem}.mp4"                               # the -o template + mp4 merge / remux fix the name
    if rc != 0:
        # a finished-but-broken output (a failed merge / fix-up) must not be taken as "already downloaded" by the
        # retry -- yt-dlp would skip the fix-up then. The .part files stay, so the retry resumes the transfer.
        for p in (tmp / f"{stem}.mp4", tmp / f"{stem}.temp.mp4"):
            try:
                p.unlink()
            except OSError:
                pass
        raise VodError(_ytdlp_error("\n".join(tail), provider))
    if not got.exists():
        raise VodError(_msg("server.vods.no_file", "yt-dlp finished without a file")["message"])
    if on_progress:
        on_progress(0.99, _msg("server.vods.checking", "checking the file")["message"])
    ensure_mp4(got)
    # the sidecar first, then the move: a library scan in between must already see the title / link
    side = {k: meta.get(k) for k in ("url", "provider", "id", "title", "channel", "channel_key", "channel_url",
                                     "start_utc", "duration_s", "was_live")}
    side["downloaded_utc"] = time.time()
    final.with_suffix(SIDECAR).write_text(json.dumps(side, indent=1), encoding="utf-8")
    # dated like the stream (or the moment the name says): never "still being written" for the library
    stamp = float(meta.get("start_utc") or datetime.strptime(stem.split("_", 2)[2][:19], "%Y-%m-%d_%H-%M-%S").timestamp())
    os.utime(got, (stamp, stamp))
    got.replace(final)                                          # same folder tree: an atomic move
    for leftover in tmp.glob(f"{stem}.*"):                     # fragments / .part of this download
        try:
            leftover.unlink()
        except OSError:
            pass
    logger.info("[VODs] downloaded %s %.1f MB in %.0fs", final, final.stat().st_size / 1e6, time.monotonic() - t0)
    return final


# ---- the job stage ----------------------------------------------------------------------------------
def run_download(ctx, library) -> str:
    """Stage 'download' (net lane): jobs.arg = the inspect() result as JSON. After the move the library
    is scanned at once, so the VOD is probed and (first probe) transcribed + detected like a recording."""
    try:
        meta = json.loads(ctx.job.get("arg") or "{}")
    except ValueError:
        raise VodError("this download job has no link")
    if not meta.get("url"):
        raise VodError("this download job has no link")
    cfg = ctx.cfg
    if not meta.get("provider") or not meta.get("id"):
        ctx.progress(0.01, _msg("server.vods.asking", "asking the site about the link")["message"])
        meta = inspect(cfg, meta["url"])
    sweep_tmp(cfg, ctx.db)
    have = existing(cfg, meta["provider"], meta["id"])
    if have is not None:
        final = have
        msg = f"already imported: {final.name}"
    else:
        ctx.progress(0.01, f"{meta['provider']}: {meta.get('title') or meta['id']}")
        stem = file_stem(meta["provider"], meta.get("start_utc"), meta.get("id"))
        try:
            final = download(cfg, meta, on_progress=ctx.progress, stop=ctx.stopping,
                             reserved_bytes=_reserved_by_others(ctx, provider_dir(cfg, meta["provider"])))
        except VodError:
            if ctx.stopping() and not ctx.queue._stop.is_set():
                # Stop pressed: abandon this download's parts (an app shutdown keeps them: the job resumes)
                for p in (provider_dir(cfg, meta["provider"]) / TMP_DIR).glob(f"{stem}.*"):
                    try:
                        p.unlink()
                    except OSError:
                        pass
            raise
        msg = (f"{meta.get('title') or meta['id']} -> {final} ({final.stat().st_size / 1e9:.2f} GB, "
               f"{meta.get('height') or '?'}p)")
    ensure_library_root(cfg, ctx)
    try:
        library.scan()
    except Exception as e:                                      # noqa: BLE001 -- the next watcher pass finds it
        logger.warning("[VODs] library scan after download failed: %s", e)
    row = ctx.db.one("SELECT id FROM recordings WHERE path=?", (str(final),))
    if row is None:
        msg += " | NOT in the library yet: " + _why_not_in_library(cfg, final)
    ctx.bus.publish("vods", action="downloaded", path=str(final), provider=meta["provider"], title=meta.get("title"),
                    rec_id=row["id"] if row else None)
    return msg


def _why_not_in_library(cfg, final: Path) -> str:
    root = vod_root(cfg)
    in_roots = any(os.path.normcase(os.path.normpath(r)) == os.path.normcase(os.path.normpath(str(root)))
                   for r in cfg.library.roots)
    if not in_roots:
        return f"{root} is not a library root (Settings > Library > roots, or VODs > add to library)"
    if not cfg.library.recursive:
        return "the library scan is not recursive (Settings > Library > include subfolders)"
    from core import settings as sm
    import fnmatch
    if not any(fnmatch.fnmatch(final.name.lower(), p) for p in sm.patterns(cfg)):
        return f"the file patterns ({cfg.library.patterns}) do not match .mp4"
    return "the next library scan picks it up"


def _reserved_by_others(ctx, pdir: Path) -> float:
    """Bytes other running downloads on the same drive will still need (only with vods.parallel > 1)."""
    drive = os.path.splitdrive(str(pdir))[0].upper()
    total = 0.0
    for row in ctx.db.query("SELECT id, arg FROM jobs WHERE stage='download' AND state='running' AND id != ?",
                            (ctx.job["id"],)):
        try:
            a = json.loads(row["arg"] or "{}")
        except ValueError:
            continue
        other = provider_dir(ctx.cfg, a.get("provider") or "x", create=False)
        if os.path.splitdrive(str(other))[0].upper() == drive and a.get("size_bytes"):
            total += space_needed(float(a["size_bytes"])) - 2e9
    return total


def sweep_tmp(cfg, db, max_age_days: float = 7.0) -> int:
    """Leftovers in the providers' _downloading folders (a download that was cleared, or failed for good):
    files older than max_age_days that no queued / running download job still owns."""
    owned = set()
    for row in db.query("SELECT arg FROM jobs WHERE stage='download' AND state IN ('queued','running')"):
        try:
            a = json.loads(row["arg"] or "{}")
            owned.add(file_stem(a.get("provider") or "", a.get("start_utc"), a.get("id")))
        except (ValueError, TypeError):
            pass
    n, cutoff = 0, time.time() - max_age_days * 86400
    root = vod_root(cfg)
    for prov in PROVIDER_NAMES:
        tmp = root / prov / TMP_DIR
        if not tmp.is_dir():
            continue
        for p in tmp.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < cutoff and p.name.split(".")[0] not in owned:
                    p.unlink()
                    n += 1
            except OSError:
                pass
    if n:
        logger.info("[VODs] removed %d stale file(s) from _downloading", n)
    return n


def ensure_library_root(cfg, ctx=None) -> bool:
    """The VOD root becomes a library root (Settings > Library) on the first import."""
    if not cfg.vods.add_to_library:
        return False
    root = str(vod_root(cfg))
    if any(os.path.normcase(os.path.normpath(r)) == os.path.normcase(os.path.normpath(root)) for r in cfg.library.roots):
        return False
    cfg.library.roots = list(cfg.library.roots) + [root]
    try:
        from core import settings as sm
        sm.save(cfg)
    except Exception as e:                                      # noqa: BLE001
        logger.warning("[VODs] could not save the library roots: %s", e)
    logger.info("[VODs] library root added %s", root)
    return True
