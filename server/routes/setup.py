"""First-run setup wizard + add-on packs + release check.

/api/setup (GET = what this PC has + the current choices + recommendations; POST = save them, start the chosen
downloads, mark setup done and scan the library), /api/system/pick-folder (a native folder dialog on THIS machine,
local clients only), /api/system/check-folder, /api/packs*, /api/updates. Settings are saved through the same
apply_patch + ctx.save_settings path as /api/settings, with the same side effects (a library change publishes
`library`, an STT model change unloads the engine), so the library watcher reads the new roots on its next pass.
"""

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from core import packs
from core import settings as settings_mod
from server.routes.settings import settings_view
from utils import obs, paths

logger = logging.getLogger("wclips.setup")

VIDEO_EXTS = (".mp4", ".mkv", ".mov")
CHECK_MAX_ENTRIES = 2000
PICK_TIMEOUT_S = 600
UPDATE_EVERY_S = 24 * 3600
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
        raise HTTPException(400, f"{key}: a list expected")
    return [str(x).strip() for x in v if str(x).strip()]


def version_tuple(v) -> tuple:
    """'v0.2.1' -> (0, 2, 1); a pre-release tail ('0.3.0-beta1') sorts below the plain release."""
    s = str(v or "").strip().lstrip("vV")
    m = re.match(r"(\d+(?:\.\d+)*)(.*)", s)
    if not m:
        return ()
    nums = tuple(int(x) for x in m.group(1).split("."))
    nums = nums + (0,) * (3 - len(nums)) if len(nums) < 3 else nums
    return nums + ((0,) if m.group(2).strip() else (1,))


def is_newer(latest: str, current: str) -> bool:
    lt, ct = version_tuple(latest), version_tuple(current)
    return bool(lt) and bool(ct) and lt > ct


def latest_release(repo: str, timeout: float = 5.0) -> dict:
    """{"tag", "url"} of the repo's latest GitHub release (tests replace this)."""
    import requests
    r = requests.get(f"https://api.github.com/repos/{repo}/releases/latest", timeout=timeout,
                     headers={"User-Agent": f"WardogClips/{paths.APP_VERSION}", "Accept": "application/vnd.github+json"})
    if r.status_code == 404:
        raise RuntimeError(f"no published release in {repo}")
    r.raise_for_status()
    j = r.json()
    return {"tag": str(j.get("tag_name") or ""), "url": str(j.get("html_url") or "")}


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
                "stt_enabled": bool(c.stt.enabled)}

    @r.get("/api/setup")
    def get_setup():
        st = packs.status(ctx.cfg)
        det = _detected()
        rec_model = st["recommended_model"]
        rec_packs = [p["id"] for p in st["packs"] if p["recommended"] and not p["installed"]]
        if not packs.model_cached(rec_model):
            rec_packs.append(packs.MODEL_PREFIX + rec_model)
        return {"done": bool(ctx.cfg.setup.done), "detected": det, "current": _current(),
                "recommended": {"stt_model": rec_model, "packs": rec_packs}, "packs": st}

    @r.post("/api/setup")
    def post_setup(body: dict):
        if not isinstance(body, dict):
            raise HTTPException(400, "object expected")
        install = _str_list(body.get("install"), "install")
        bad = [i for i in install if not packs._valid_id(i)]
        if bad:
            raise HTTPException(400, f"unknown add-on(s): {', '.join(bad)}")
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
                raise HTTPException(400, f"unknown speech model {m!r}")
            patch.setdefault("stt", {})["model"] = m
        if "stt_enabled" in body:
            patch.setdefault("stt", {})["enabled"] = bool(body["stt_enabled"])
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
            raise HTTPException(403, "the folder dialog opens on the WardogClips PC: local clients only")
        body = body if isinstance(body, dict) else {}
        if not _pick_lock.acquire(blocking=False):
            raise HTTPException(409, "a folder dialog is already open")
        try:
            initial = str(body.get("initial") or "").strip()
            if initial and (is_remote_path(initial) and not _known_root(ctx.cfg, initial) or len(initial) > 1024
                            or "\x00" in initial):
                logger.info("[Setup] folder dialog initial dropped (network / device path)")
                initial = ""               # Test-Path on a UNC path would authenticate to that host
            return {"path": run_folder_dialog(initial, _clean_title(body.get("title") or ""))}
        finally:
            _pick_lock.release()

    @r.get("/api/system/check-folder")
    def check_folder(path: str = ""):
        # also needs X-WardogClips: 1 (server/app.py request guard): an <img src> from another site cannot call it
        p = (path or "").strip().strip('"')
        if not p:
            raise HTTPException(400, "path missing")
        if "\x00" in p:
            raise HTTPException(400, "bad path")
        if is_remote_path(p) and not _known_root(ctx.cfg, p):
            # not touched: answered like "can't check this folder right now" (the page shows c.error that way)
            logger.info("[Setup] check-folder skipped a network / device path")
            return {"path": p, "exists": None, "is_dir": None, "n_videos": 0, "capped": False, "free_gb": None,
                    "network": True, "error": "network or device path: not checked (it is still saved as typed)"}
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
            raise HTTPException(404, f"unknown pack {pack_id!r}")
        try:
            return packs.start(pack_id, bus=ctx.bus, force=bool(force))
        except ValueError as e:
            raise HTTPException(404, str(e))

    @r.post("/api/packs/{pack_id}/cancel")
    def cancel_pack(pack_id: str):
        if not packs._valid_id(pack_id):
            raise HTTPException(404, f"unknown pack {pack_id!r}")
        return {"ok": packs.cancel(pack_id)}

    @r.post("/api/packs/{pack_id}/remove")
    def remove_pack(pack_id: str):
        if not packs._valid_id(pack_id):
            raise HTTPException(404, f"unknown pack {pack_id!r}")
        if packs.busy(pack_id):
            raise HTTPException(409, "the add-on is installing -- cancel it first")
        res = packs.remove_ex(pack_id, ctx.cfg)
        if not res.get("ok") and res.get("reason"):
            raise HTTPException(409, res["reason"])
        return {"ok": bool(res.get("ok"))}

    @r.get("/api/updates")
    def updates(force: int = 0):
        u = ctx.cfg.updates
        cur = paths.APP_VERSION
        if not u.repo:
            return {"current": cur, "latest": "", "newer": False, "url": "", "checked": u.last_check,
                    "disabled": True}
        url = f"https://github.com/{u.repo}/releases/latest"
        out = {"current": cur, "latest": u.latest, "newer": is_newer(u.latest, cur), "url": url,
               "checked": u.last_check}
        due = True
        if u.last_check:
            try:
                due = time.time() - datetime.fromisoformat(u.last_check).timestamp() >= UPDATE_EVERY_S
            except ValueError:
                due = True
        if not force and (not due or not u.check):
            if not u.check:
                out["auto"] = False
            return out
        try:
            rel = latest_release(u.repo)
        except Exception as e:                 # noqa: BLE001 -- offline / rate-limited: answer with what we had
            logger.info("[Updates] check failed repo=%s err=%s", u.repo, str(e).encode("ascii", "replace").decode())
            out["error"] = str(e)[:200]
            return out
        latest = rel.get("tag") or ""
        _save({"updates": {"last_check": _now_iso(), "latest": latest}})
        logger.info("[Updates] checked repo=%s latest=%s current=%s", u.repo, latest or "-", cur)
        return {"current": cur, "latest": latest, "newer": is_newer(latest, cur), "url": rel.get("url") or url,
                "checked": ctx.cfg.updates.last_check}

    return r


def run_folder_dialog(initial: str = "", title: str = "") -> str | None:
    """The native folder dialog through Windows PowerShell; None when cancelled / failed / timed out."""
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
