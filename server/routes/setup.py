"""First-run setup wizard + add-on packs.

/api/setup (GET = what this PC has + the current choices + recommendations; POST = save them, start the chosen
downloads, mark setup done and scan the library), /api/system/pick-folder (a native folder dialog on THIS machine,
local clients only), /api/system/check-folder, /api/packs* (the release check is server/routes/updates.py). Settings are saved through the same
apply_patch + ctx.save_settings path as /api/settings, with the same side effects (a library change publishes
`library`, an STT model change unloads the engine), so the library watcher reads the new roots on its next pass.
"""

import logging
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request

from core import packs
from core.messages import http_error
from core.updates import is_newer, version_tuple  # noqa: F401 -- kept importable from here
from core import settings as settings_mod
from core import speechlang
from server.routes.settings import settings_view
from utils import i18n, obs, paths
from utils.i18n import app_error, with_msg

logger = logging.getLogger("wclips.setup")

VIDEO_EXTS = (".mp4", ".mkv", ".mov")
CHECK_MAX_ENTRIES = 2000
PICK_TIMEOUT_S = 600
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}

_pick_lock = threading.Lock()

# FolderBrowserDialog needs an STA thread (-STA) and an owner window to come to the front: a 1x1 invisible TopMost
# form. Windows PowerShell 5.1 (.NET Framework) has no UseDescriptionForTitle, so the title goes into Description.
# The initial folder / title come in through env vars, never spliced into the script text.
PICK_SCRIPT = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.FormBorderStyle = 'None'
$owner.StartPosition = 'CenterScreen'
$owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.Opacity = 0
$owner.Show()
$owner.Activate()
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = $env:WCLIPS_PICK_TITLE
$d.ShowNewFolderButton = $true
if ($env:WCLIPS_PICK_INITIAL -and (Test-Path -LiteralPath $env:WCLIPS_PICK_INITIAL -PathType Container)) {
  $d.SelectedPath = $env:WCLIPS_PICK_INITIAL
}
$r = $d.ShowDialog($owner)
$owner.Close()
if ($r -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($d.SelectedPath) }
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _is_local(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    return host in LOCAL_HOSTS or host.startswith("::ffff:127.")


def is_remote_path(p: str) -> bool:
    r"""True for a path Windows would open over the network or as a raw device: \\server\share, //server/share,
    \\?\UNC\..., \\?\C:\..., \\.\PhysicalDrive0 (any path starting with two separators), the NT pass-through
    \??\..., and any drive that is not a plain letter. Touching a UNC path makes Windows authenticate to that host
    as the user (the NTLM hash leaks) and an unreachable host pins a worker thread for 20-60 s."""
    t = (p or "").strip().strip('"').strip()
    if len(t) >= 2 and t[0] in "\\/" and t[1] in "\\/":
        return True
    if t[:4].replace("/", "\\") == "\\??\\":
        return True
    drive, _rest = os.path.splitdrive(t)
    return bool(drive) and not (len(drive) == 2 and drive[1] == ":")


def _norm_key(p: str) -> str:
    return os.path.normcase(os.path.normpath((p or "").strip().strip('"').strip())).rstrip("\\/")


def _known_root(cfg, p: str) -> bool:
    """A network path the user already made a library root may be checked (they chose to read it)."""
    k = _norm_key(p)
    try:
        return any(_norm_key(r) == k for r in cfg.library.roots if r)
    except Exception:                          # noqa: BLE001
        return False


def _clean_title(t: str, n: int = 200) -> str:
    """The dialog title: printable text only, capped (it reaches PowerShell through an env var, never the script)."""
    return "".join(ch for ch in str(t or "") if ch.isprintable())[:n].strip()


def _str_list(v, key: str) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = [x for x in re.split(r"[;\n]", v)]
    if not isinstance(v, list):
        raise app_error(400, "server.bad_request", "The request was not understood.")
    return [str(x).strip() for x in v if str(x).strip()]


def count_videos(path: str, cap: int = CHECK_MAX_ENTRIES) -> tuple[int, bool]:
    """Videos under `path`, looking at no more than `cap` directory entries in all (an HDD archive must not be walked
    whole for a wizard row). Returns (n, capped). Read-only: scandir only."""
    n = seen = 0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    seen += 1
                    if seen > cap:
                        return n, True
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.name.lower().endswith(VIDEO_EXTS):
                            n += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return n, False


def _free_gb(path: str) -> float | None:
    p = Path(path)
    for cand in [p, *p.parents]:      # a folder that does not exist yet (the VOD folder): its drive's free space
        try:
            if cand.exists():
                return round(shutil.disk_usage(str(cand)).free / 1e9, 1)
        except OSError:
            continue
    return None


def build(ctx) -> APIRouter:
    r = APIRouter()

    def _save(patch: dict) -> list[str]:
        changed = settings_mod.apply_patch(ctx.cfg, patch)
        ctx.save_settings()
        if changed:
            logger.info("[Setup] settings changed=%s", ",".join(changed))
        if any(c.startswith("library.") for c in changed):
            ctx.bus.publish("library", action="settings")
        if any(c in ("stt.model", "stt.compute") for c in changed):
            try:
                from stt import whisper as stt
                stt.engine.unload()            # the next transcribe job loads the new model
            except Exception:                  # noqa: BLE001
                logger.debug("[Setup] engine unload failed", exc_info=True)
        return changed

    def _gpus() -> list:
        """Every graphics card (name, brand, memory) for the wizard's wording (utils/gpus.py)."""
        try:
            from utils import gpus
            return [{"name": a["name"], "vendor": a["vendor"], "vram_mb": a["vram_mb"], "integrated": a["integrated"]}
                    for a in gpus.hardware()]
        except Exception:                      # noqa: BLE001
            return []

    def _detected() -> dict:
        base = obs.obs_dir()
        prof = obs.active_profile_dir(base) if base else None
        try:
            from media import ffmpeg
            hw = ffmpeg.hw_info()
        except Exception:                      # noqa: BLE001
            hw = {}
        try:
            from resolve.link import resolve_exe
            exe = resolve_exe(ctx.cfg)
        except Exception:                      # noqa: BLE001
            exe = ""
        return {
            "obs": {"found": base is not None, "profile": prof.name if prof else "",
                    "rec_paths": obs.recording_paths(base) if base else [],
                    "track_names": obs.track_names(base) if base else []},
            "gpu": packs.gpu_info(),
            "gpus": _gpus(),
            "ffmpeg": {"ok": bool(paths.ffmpeg_exe() and paths.ffprobe_exe()), "version": hw.get("version") or "",
                       "nvenc": bool(hw.get("h264_nvenc")), "cuda": bool(hw.get("cuda"))},
            "resolve": {"installed": bool(exe) and os.path.isfile(exe), "exe": exe or ""},
            "node": packs._which("node") is not None,
            "deno": packs._which("deno") is not None or packs.deno_exe() is not None,
        }

    def _current() -> dict:
        c = ctx.cfg
        try:
            from media import vods
            eff = str(vods.vod_root(c))
        except Exception:                      # noqa: BLE001
            eff = c.vods.dir
        return {"roots": list(c.library.roots), "aliases": list(c.player.aliases), "squad": list(c.player.squad),
                "vods_dir": c.vods.dir, "vods_dir_effective": eff, "stt_model": c.stt.model,
                "stt_enabled": bool(c.stt.enabled), "stt_language": c.stt.language}

    @r.get("/api/setup")
    def get_setup():
        st = packs.status(ctx.cfg)
        det = _detected()
        rec_model = st["recommended_model"]
        rec_packs = [p["id"] for p in st["packs"] if p["recommended"] and not p["installed"]]
        # a first run starts from the UI language (when the app has one); a re-run from the speech language in use
        ui_lang = getattr(getattr(ctx.cfg, "ui", None), "language", "") or ""
        rec_lang = ctx.cfg.stt.language if ctx.cfg.setup.done else speechlang.default_speech_language(ui_lang)
        eff_model = speechlang.effective_model(rec_model, rec_lang)
        if not packs.model_cached(eff_model):
            rec_packs.append(packs.MODEL_PREFIX + eff_model)
        return {"done": bool(ctx.cfg.setup.done), "detected": det, "current": _current(),
                "recommended": {"stt_model": rec_model, "packs": rec_packs, "stt_language": rec_lang},
                "speech_languages": speechlang.catalog(), "packs": st}

    @r.post("/api/setup")
    def post_setup(body: dict):
        if not isinstance(body, dict):
            raise app_error(400, "server.bad_request", "The request was not understood.")
        install = _str_list(body.get("install"), "install")
        bad = [i for i in install if not packs._valid_id(i)]
        if bad:
            raise app_error(400, "server.packs.unknown_list", "Unknown add-on: {ids}", ids=", ".join(bad))
        patch: dict = {}
        if "roots" in body:
            patch.setdefault("library", {})["roots"] = _str_list(body["roots"], "roots")
        if "aliases" in body:
            patch.setdefault("player", {})["aliases"] = _str_list(body["aliases"], "aliases")
        if "squad" in body:
            patch.setdefault("player", {})["squad"] = _str_list(body["squad"], "squad")
        if "vods_dir" in body:
            patch.setdefault("vods", {})["dir"] = str(body["vods_dir"] or "").strip()
        if "stt_model" in body:
            m = str(body["stt_model"] or "").strip()
            if m not in settings_mod.STT_MODELS:
                raise app_error(400, "server.setup.unknown_model", "Unknown speech model: {model}", model=m)
            patch.setdefault("stt", {})["model"] = m
        if "stt_enabled" in body:
            patch.setdefault("stt", {})["enabled"] = bool(body["stt_enabled"])
        if "stt_language" in body:
            patch.setdefault("stt", {})["language"] = speechlang.normalize(body["stt_language"])
        done = bool(body.get("done"))
        if done:
            patch["setup"] = {"done": True, "completed_at": _now_iso()}
        changed = _save(patch) if patch else []
        started = []
        for pid in install:
            cur = packs._status_one(pid)
            if cur.get("installed") and pid != "ytdlp":
                continue                       # already there: no second download of 1 GB
            try:
                packs.start(pid, bus=ctx.bus)
                started.append(pid)
            except Exception as e:             # noqa: BLE001
                logger.warning("[Setup] could not start pack=%s err=%s", pid, e)
        if done:
            ctx.bus.publish("setup", done=True)
            threading.Thread(target=_scan, name="setup-scan", daemon=True).start()
        logger.info("[Setup] saved done=%s changed=%d started=%s", done, len(changed), ",".join(started) or "-")
        return {"ok": True, "started": started, "changed": changed, "settings": settings_view(ctx.cfg)}

    def _scan():
        try:
            s = ctx.library.scan()
            logger.info("[Setup] library scan found=%s new=%s", s.get("found"), s.get("new"))
        except Exception:                      # noqa: BLE001
            logger.warning("[Setup] library scan failed", exc_info=True)

    @r.post("/api/system/pick-folder")
    def pick_folder(request: Request, body: dict | None = None):
        if not _is_local(request):
            raise app_error(403, "server.setup.pick_local_only", "The folder dialog opens on the PC that runs WardogClips, so it works from that PC only.")
        body = body if isinstance(body, dict) else {}
        if not _pick_lock.acquire(blocking=False):
            raise app_error(409, "server.setup.pick_busy", "A folder dialog is already open.")
        try:
            initial = str(body.get("initial") or "").strip()
            if initial and (is_remote_path(initial) and not _known_root(ctx.cfg, initial) or len(initial) > 1024
                            or "\x00" in initial):
                logger.info("[Setup] folder dialog initial dropped (network / device path)")
                initial = ""               # Test-Path on a UNC path would authenticate to that host
            title = _clean_title(body.get("title") or "") or i18n.fmt(i18n.lookup(i18n.active_language(ctx.cfg), "server.setup.pick_title") or "Choose a folder")
            return {"path": run_folder_dialog(initial, title)}
        finally:
            _pick_lock.release()

    @r.get("/api/system/check-folder")
    def check_folder(path: str = ""):
        # also needs X-WardogClips: 1 (server/app.py request guard): an <img src> from another site cannot call it
        p = (path or "").strip().strip('"')
        if not p:
            raise app_error(400, "server.setup.path_missing", "Enter a folder.")
        if "\x00" in p:
            raise app_error(400, "server.setup.bad_path", "That is not a valid folder path.")
        if is_remote_path(p) and not _known_root(ctx.cfg, p):
            # not touched: answered like "can't check this folder right now" (the page shows c.error that way)
            logger.info("[Setup] check-folder skipped a network / device path")
            return with_msg({"path": p, "exists": None, "is_dir": None, "n_videos": 0, "capped": False, "free_gb": None,
                             "network": True}, "error", "server.setup.network_path",
                            "This is a network or device path, so it was not checked. It is still saved as you typed it.")
        exists = os.path.exists(p)
        is_dir = os.path.isdir(p)
        n, capped = count_videos(p) if is_dir else (0, False)
        return {"path": p, "exists": exists, "is_dir": is_dir, "n_videos": n, "capped": capped,
                "free_gb": _free_gb(p)}

    @r.get("/api/packs")
    def get_packs():
        return packs.status(ctx.cfg)

    @r.post("/api/packs/{pack_id}/install")
    def install_pack(pack_id: str, force: int = 0):
        if not packs._valid_id(pack_id):
            raise app_error(404, "server.packs.unknown", "Unknown add-on: {id}", id=pack_id)
        try:
            return packs.start(pack_id, bus=ctx.bus, force=bool(force))
        except ValueError as e:
            raise http_error(404, e)

    @r.post("/api/packs/{pack_id}/cancel")
    def cancel_pack(pack_id: str):
        if not packs._valid_id(pack_id):
            raise app_error(404, "server.packs.unknown", "Unknown add-on: {id}", id=pack_id)
        return {"ok": packs.cancel(pack_id)}

    @r.post("/api/packs/{pack_id}/remove")
    def remove_pack(pack_id: str):
        if not packs._valid_id(pack_id):
            raise app_error(404, "server.packs.unknown", "Unknown add-on: {id}", id=pack_id)
        if packs.busy(pack_id):
            raise app_error(409, "server.packs.installing", "It is installing right now. Cancel the install first.")
        res = packs.remove_ex(pack_id, ctx.cfg)
        if not res.get("ok") and res.get("reason"):
            raise http_error(409, packs.reason_error(res))
        return {"ok": bool(res.get("ok"))}

    return r


def run_folder_dialog(initial: str = "", title: str = "") -> str | None:
    """The native folder dialog through Windows PowerShell; None when canceled / failed / timed out."""
    exe = shutil.which("powershell") or os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                                    "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    env = dict(os.environ, WCLIPS_PICK_INITIAL=initial, WCLIPS_PICK_TITLE=title or "Choose a folder")
    try:
        p = subprocess.run([exe, "-STA", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-Command", PICK_SCRIPT], capture_output=True, timeout=PICK_TIMEOUT_S, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        logger.warning("[Setup] folder dialog timed out after %ds", PICK_TIMEOUT_S)
        return None
    except OSError as e:
        logger.warning("[Setup] folder dialog could not start err=%s", e)
        return None
    path = (p.stdout or b"").decode("utf-8", "replace").strip().lstrip("\ufeff")
    if p.returncode != 0 and not path:
        logger.warning("[Setup] folder dialog failed rc=%s err=%s", p.returncode,
                       (p.stderr or b"").decode("utf-8", "replace").strip()[:200].encode("ascii", "replace").decode())
    return path or None
