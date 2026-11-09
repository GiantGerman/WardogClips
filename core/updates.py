"""Update check + "Update now": GitHub releases of settings.updates.repo, a prompt in the page / tray, and the hand-off
to the new installer.

- Check: ``GET https://api.github.com/repos/<repo>/releases/latest`` (channel "beta": the newest of
  ``/releases``, pre-releases included; drafts never). A release is offered when its tag compares higher than
  ``paths.APP_VERSION`` and is not ``updates.skipped_version``. Its installer is the asset
  ``WardogClips-Setup-<ver>.exe``; its sha256 comes from a ``<installer>.sha256`` asset beside it, GitHub's own asset
  digest, or a sha256 written in the release notes (in that order). A 404 (private repo / no release), a rate limit,
  no network or an unreadable answer is a quiet "could not check" state with a plain reason, never an error popup.
- Automatic check (``Updater.start``, server runs only -- never ``--scan``, never tests): once ~STARTUP_DELAY_S
  after start, then every ``updates.interval_hours`` while ``updates.auto_check`` is on and a repo is set.
- Prompt: every state change is published on the bus as SSE ``updates``; the page shows a non-blocking card
  (Update now / Later / Skip this version), the tray a notification + an "Update available..." menu entry. Later
  only hides the prompt until the next check; Skip saves ``updates.skipped_version``.
- Update now (installed builds only; a source run gets the release page instead): the installer is streamed to
  ``<data dir>\\updates\\`` with core/packs.py's downloader (Range resume, size + sha256 checked) from https hosts
  in ALLOWED_HOSTS only, every redirect hop checked. Without any checksum it only runs after a second confirmation.
  Then the installer is started (its normal, visible wizard) and this app shuts down through the same path as
  POST /api/system/quit; the installer's own launcher --quit step finds nothing running (or waits for it).

Nothing here runs a file without the user's click on "Update now". No tokens: a private repository cannot be
checked (it answers 404) until its releases are public.
"""

import hashlib
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from utils import paths
from utils.i18n import msg

logger = logging.getLogger("wclips.updates")

API = "https://api.github.com"
ALLOWED_HOSTS = frozenset({"api.github.com", "github.com", "objects.githubusercontent.com",
                           "release-assets.githubusercontent.com"})
CHANNELS = ("stable", "beta")
INSTALLER_FMT = "WardogClips-Setup-{ver}.exe"
STARTUP_DELAY_S = 45.0           # the first automatic check waits until the app has settled
RETRY_S = 3600.0                 # after a failed check, the next automatic try comes no sooner than this
TICK_S = 60.0                    # the loop re-reads the settings this often (a toggle applies within a minute)
TIMEOUT = (10, 20)
NOTES_MAX = 6000                 # characters of release notes kept for the prompt
SHA_FILE_MAX = 4096
INSTALLER_MAX = 1 << 30          # 1 GB: anything bigger is not our installer
HANDOFF_DELAY_S = 1.5            # the installer window comes up before this app starts shutting down
PART_MAX_AGE_S = 7 * 86400
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
SHA_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")

# plain reasons shown in the page: English text + the catalog key server.update.<code> (reason_i18n)
MSG = {
    "not_found": msg("server.update.not_found",
               "The update page could not be found. The releases may be private, "
               "or none are published yet.")["message"],
    "rate_limited": msg("server.update.rate_limited",
               "GitHub is limiting update checks right now. WardogClips "
               "will try again later.")["message"],
    "offline": msg("server.update.offline", "Could not reach GitHub. Check your internet connection.")["message"],
    "bad_response": msg("server.update.bad_response",
               "GitHub sent an answer WardogClips could not read. It "
               "will try again later.")["message"],
    "http_error": msg("server.update.http_error",
               "GitHub answered with an error. WardogClips will try "
               "again later.")["message"],
    "no_release": msg("server.update.no_release", "No published release was found.")["message"],
    "bad_repo": msg("server.update.bad_repo",
               "The update source in Settings > Updates is not a "
               "valid owner/name.")["message"],
    "disabled": msg("server.update.disabled",
               "No update source is set, so WardogClips does not "
               "check for updates.")["message"],
    "no_asset": msg("server.update.no_asset",
               "This release has no Windows installer to download. Use the "
               "download page instead.")["message"],
    "not_installed": msg("server.update.not_installed",
               "Automatic updating works in the installed version only. Use "
               "the download page instead.")["message"],
    "bad_checksum": msg("server.update.bad_checksum",
               "The downloaded file did not match its checksum, so it was "
               "deleted. Try again later.")["message"],
    "checksum_unreadable": msg("server.update.checksum_unreadable",
               "The checksum file of this release could not be read, so "
               "nothing was installed.")["message"],
    "too_big": msg("server.update.too_big",
               "The installer of this release is larger than expected, so "
               "it was not downloaded.")["message"],
    "download_failed": msg("server.update.download_failed",
               "The download did not finish. Try again to "
               "resume it.")["message"],
    "cancelled": msg("server.update.cancelled", "The download was canceled.")["message"],
    "launch_failed": msg("server.update.launch_failed",
               "The installer could not be started. Open the download page "
               "and run it yourself.")["message"],
    "busy": msg("server.update.busy", "An update is already downloading.")["message"],
    "nothing": msg("server.update.nothing", "There is no update to install.")["message"],
    "need_confirm": msg("server.update.need_confirm",
               "This release has no checksum, so WardogClips cannot verify the download. "
               "Install it only if you trust where it came from.")["message"],
}


def reason_i18n(code: str) -> dict | None:
    """{code, params} of a reason code's catalog text for the page (srvText), None for no / an unknown code."""
    return {"code": "server.update." + code, "params": {}} if code in MSG else None


class CheckError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _ascii(s, n: int = 200) -> str:
    return str(s or "")[:n].encode("ascii", "replace").decode("ascii")


def updates_dir() -> Path:
    return Path(paths.DATA_DIR) / "updates"     # read at call time: tests point DATA_DIR at a temp dir


# ---- versions -----------------------------------------------------------------------------------------------------------
def version_tuple(v) -> tuple:
    """'v0.2.1' -> ((0, 2, 1), 1); a pre-release tail ('0.3.0-beta1') sorts below the plain release, and two
    pre-releases of one version compare by their tails ('beta2' > 'beta1', 'rc1' > 'beta3', 'beta10' > 'beta9')."""
    s = str(v or "").strip().lstrip("vV")
    m = re.match(r"(\d+(?:\.\d+)*)(.*)", s)
    if not m:
        return ()
    nums = [int(x) for x in m.group(1).split(".")]
    while len(nums) > 3 and nums[-1] == 0:      # 0.3.0.0 == 0.3.0
        nums.pop()
    nums += [0] * (3 - len(nums))
    tail = m.group(2).strip()
    if not tail:
        return (tuple(nums), 1)
    key = tuple((0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in re.findall(r"\d+|[A-Za-z]+", tail))
    return (tuple(nums), 0, key)


def is_newer(latest: str, current: str) -> bool:
    lt, ct = version_tuple(latest), version_tuple(current)
    return bool(lt) and bool(ct) and lt > ct


def clean_version(tag: str) -> str:
    return str(tag or "").strip().lstrip("vV")


# ---- network (hosts allow-listed, https only, redirects followed by hand) -----------------------------------------------
def host_ok(url: str) -> bool:
    u = urlparse(url)
    return u.scheme == "https" and (u.hostname or "").lower() in ALLOWED_HOSTS


def _check(url: str) -> None:
    if not host_ok(url):
        from core.packs import PackError
        raise PackError(f"refused update host {_ascii(urlparse(url).hostname or url, 80)!r}")


def make_session():
    """A requests session that refuses any request outside ALLOWED_HOSTS (core/packs.py's downloader adds its own
    hop-by-hop redirect check on top, with allow_redirects=False)."""
    import requests

    class _Session(requests.Session):
        def request(self, method, url, *a, **kw):
            _check(url)
            kw["allow_redirects"] = False
            return super().request(method, url, *a, **kw)

    s = _Session()
    s.headers["User-Agent"] = f"WardogClips/{paths.APP_VERSION}"
    s.trust_env = False                      # no proxy / netrc credentials from the environment
    return s


def _get(sess, url: str, headers: dict | None = None, stream: bool = False):
    for _ in range(6):
        _check(url)
        r = sess.get(url, headers=headers or {}, stream=stream, allow_redirects=False, timeout=TIMEOUT)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            url = urljoin(url, r.headers["Location"])
            r.close()
            continue
        return r
    raise CheckError("bad_response", "too many redirects")


def _api_json(sess, path: str):
    url = API + path
    try:
        r = _get(sess, url, headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    except CheckError:
        raise
    except Exception as e:                   # noqa: BLE001 -- connection refused / DNS / timeout / refused host
        name = type(e).__name__
        if name == "PackError":
            raise CheckError("bad_response", str(e)) from e
        raise CheckError("offline", f"{name}: {e}") from e
    try:
        if r.status_code == 404:
            raise CheckError("not_found", "HTTP 404")
        if r.status_code in (403, 429):
            rem = str(r.headers.get("X-RateLimit-Remaining", ""))
            if r.status_code == 429 or rem == "0" or "rate limit" in (getattr(r, "text", "") or "").lower():
                raise CheckError("rate_limited", f"HTTP {r.status_code}")
            raise CheckError("not_found", f"HTTP {r.status_code}")   # forbidden = private for us
        if r.status_code != 200:
            raise CheckError("http_error", f"HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise CheckError("bad_response", "not JSON") from e
    finally:
        try:
            r.close()
        except Exception:                    # noqa: BLE001
            pass


# ---- release parsing ----------------------------------------------------------------------------------------------------
def notes_plain(md: str, limit: int = NOTES_MAX) -> str:
    """Release notes (GitHub markdown) as plain text for a textContent: no markup, no HTML, no images, links as their
    text. The page never parses it as HTML."""
    s = str(md or "").replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"```.*?```", lambda m: m.group(0).strip("`").strip("\n"), s, flags=re.S)
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)                     # images
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)                 # [text](url) -> text
    s = re.sub(r"<[^>\n]{1,200}>", "", s)                          # inline html tags
    out = []
    for line in s.split("\n"):
        t = line.rstrip()
        t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t)                    # headings
        t = re.sub(r"^\s*[-*+]\s+", "- ", t)                       # bullets
        t = re.sub(r"^\s*>\s?", "", t)                             # quotes
        t = re.sub(r"(\*\*|__)(.+?)\1", r"\2", t)
        t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", t)
        t = t.replace("`", "")
        if re.fullmatch(r"\s*([-*_]\s*){3,}", t):
            t = ""
        out.append(t)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ch.isprintable())
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n..."
    return text


def body_sha256(body: str, installer: str) -> str:
    """A sha256 written in the release notes: on the line naming the installer, else the only one in the text."""
    text = str(body or "")
    for line in text.splitlines():
        if installer.lower() in line.lower():
            m = SHA_RE.search(line)
            if m:
                return m.group(0).lower()
    found = {m.group(0).lower() for m in SHA_RE.finditer(text)}
    return found.pop() if len(found) == 1 else ""


def parse_release(j: dict) -> dict:
    """The fields the prompt + the download need from one GitHub release object."""
    if not isinstance(j, dict):
        raise CheckError("bad_response", "release is not an object")
    tag = str(j.get("tag_name") or "")
    ver = clean_version(tag)
    if not version_tuple(ver):
        raise CheckError("bad_response", f"tag {_ascii(tag, 40)!r} is not a version")
    want = INSTALLER_FMT.format(ver=ver).lower()
    inst = sha_asset = None
    for a in j.get("assets") or []:
        if not isinstance(a, dict):
            continue
        n = str(a.get("name") or "").lower()
        if n == want:
            inst = a
        elif n == want + ".sha256":
            sha_asset = a
    body = str(j.get("body") or "")
    out = {"tag": tag, "version": ver, "name": str(j.get("name") or "") or f"WardogClips {ver}",
           "url": str(j.get("html_url") or ""), "published": str(j.get("published_at") or ""),
           "prerelease": bool(j.get("prerelease")), "draft": bool(j.get("draft")),
           "notes": notes_plain(body), "asset": None, "sha256": "", "sha256_url": "", "checksum": ""}
    if not out["url"].startswith("https://github.com/"):
        out["url"] = ""
    if inst is not None:
        url = str(inst.get("browser_download_url") or "")
        out["asset"] = {"name": str(inst.get("name") or ""), "url": url if host_ok(url) else "",
                        "size": int(inst.get("size") or 0)}
        dig = str(inst.get("digest") or "")
        if sha_asset is not None and host_ok(str(sha_asset.get("browser_download_url") or "")):
            out["sha256_url"], out["checksum"] = str(sha_asset["browser_download_url"]), "file"
        elif dig.lower().startswith("sha256:") and SHA_RE.fullmatch(dig[7:]):
            out["sha256"], out["checksum"] = dig[7:].lower(), "github"
        else:
            sha = body_sha256(body, want)
            if sha:
                out["sha256"], out["checksum"] = sha, "notes"
    return out


def fetch_release(sess, repo: str, channel: str = "stable") -> dict:
    """The newest release the channel allows, parsed. Raises CheckError."""
    if not REPO_RE.match(repo or ""):
        raise CheckError("bad_repo", repo)
    if channel == "beta":
        lst = _api_json(sess, f"/repos/{repo}/releases?per_page=20")
        if not isinstance(lst, list):
            raise CheckError("bad_response", "release list is not a list")
        best = None
        for j in lst:
            if not isinstance(j, dict) or j.get("draft"):
                continue
            try:
                rel = parse_release(j)
            except CheckError:
                continue
            if best is None or version_tuple(rel["version"]) > version_tuple(best["version"]):
                best = rel
        if best is None:
            raise CheckError("no_release")
        return best
    rel = parse_release(_api_json(sess, f"/repos/{repo}/releases/latest"))
    if rel["draft"] or rel["prerelease"]:
        raise CheckError("no_release", "the latest release is a draft or a pre-release")
    return rel


# ---- scheduling ---------------------------------------------------------------------------------------------------------
def _epoch(iso: str) -> float | None:
    try:
        return datetime.fromisoformat(str(iso)).timestamp() if iso else None
    except ValueError:
        return None


def is_due(u, now: float, last_attempt: float | None = None, last_failed: bool = False) -> bool:
    """Whether the automatic check should run now (u = settings.updates)."""
    if not (u.auto_check and u.repo):
        return False
    if last_failed and last_attempt is not None and now - last_attempt < RETRY_S:
        return False
    last = _epoch(u.last_check)
    return last is None or now - last >= max(1, int(u.interval_hours)) * 3600 or now < last


def auto_disabled() -> bool:
    """The test suite (and anyone who wants no network at all) sets WARDOGCLIPS_NO_UPDATE_CHECK=1."""
    return os.environ.get("WARDOGCLIPS_NO_UPDATE_CHECK", "").strip() not in ("", "0")


# ---- the download job (duck-typed like core/packs.py's _Job for its _stream helper) -----------------------------------
class _DlJob:
    def __init__(self, owner: "Updater"):
        self.owner = owner
        self.cancel = threading.Event()
        self.bytes = 0
        self.total = 0
        self._last = 0.0

    def set(self, force: bool = False, **kw) -> None:
        if "bytes" in kw:
            self.bytes = int(kw["bytes"])
            if self.bytes > INSTALLER_MAX:     # a release without a size must not fill the disk
                raise CheckError("too_big", f"over {INSTALLER_MAX} bytes")
        now = time.monotonic()
        if force or now - self._last >= 0.25:
            self._last = now
            self.owner._progress(self.bytes, self.total)


# ---- the updater ---------------------------------------------------------------------------------------------------------
class Updater:
    """One per Ctx. State in memory (+ updates.last_check / latest / skipped_version in the settings)."""

    def __init__(self, ctx, session_factory=None):
        self.ctx = ctx
        self.session_factory = session_factory or make_session
        self._lock = threading.RLock()
        self._check_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.thread: threading.Thread | None = None
        self.listeners: list = []          # callables(state dict): the tray
        self.release: dict | None = None
        self.release_key: tuple = ()       # (repo, channel) the release came from
        self.state = "idle"                # idle | checking | up_to_date | available | could_not_check | disabled
        #                                    | downloading | verifying | starting | failed
        self.reason_code = ""
        self.detail = ""
        self.dismissed = ""                # "Later": hidden until the next check
        self.bytes = 0
        self.total = 0
        self.last_attempt: float | None = None
        self.last_failed = False
        self._job: _DlJob | None = None
        self._dl_thread: threading.Thread | None = None
        self.launched = ""

    # -- view --------------------------------------------------------------------------------------------------
    def view(self) -> dict:
        u = self.ctx.cfg.updates
        cur = paths.APP_VERSION
        with self._lock:
            rel = self.release or {}
            latest = rel.get("version") or clean_version(u.latest)
            newer = is_newer(latest, cur)
            skipped = bool(latest) and clean_version(u.skipped_version) == latest
            state = self.state
            if not u.repo and state not in ("downloading", "verifying", "starting"):
                state = "disabled"
            asset = rel.get("asset") or None
            installed = bool(paths.INSTALLED)
            url = rel.get("url") or (f"https://github.com/{u.repo}/releases/latest" if REPO_RE.match(u.repo or "") else "")
            out = {
                "current": cur, "latest": latest, "newer": newer, "url": url, "checked": u.last_check,
                "state": state, "reason_code": self.reason_code,
                "reason": MSG.get(self.reason_code, "") if self.reason_code else "",
                "reason_i18n": reason_i18n(self.reason_code),
                "name": rel.get("name") or "", "notes": rel.get("notes") or "", "published": rel.get("published") or "",
                "prerelease": bool(rel.get("prerelease")), "skipped": skipped, "skipped_version": u.skipped_version,
                "auto": bool(u.auto_check), "channel": u.channel, "interval_hours": u.interval_hours,
                "known": self.release is not None,   # False = only the version saved by an earlier run is known
                "installed_build": installed, "has_installer": bool(asset and asset.get("url")),
                "has_checksum": bool(rel.get("checksum")), "checksum": rel.get("checksum") or "",
                "size": int((asset or {}).get("size") or 0),
                "can_install": installed and bool(asset and asset.get("url")),
                "bytes": self.bytes, "total": self.total,
                "progress": (self.bytes / self.total) if self.total else 0.0,
                "prompt": (state == "available" and newer and not skipped and self.dismissed != latest),
            }
            if not u.repo:
                out["disabled"] = True
            if state == "could_not_check":
                out["error"] = out["reason"]
                out["error_i18n"] = out["reason_i18n"]
            return out

    def _publish(self) -> None:
        v = self.view()
        try:
            self.ctx.bus.publish("updates", **v)
        except Exception:                    # noqa: BLE001 -- a bus problem never breaks the updater
            logger.debug("[Updates] publish failed", exc_info=True)
        for cb in list(self.listeners):
            try:
                cb(v)
            except Exception:                # noqa: BLE001
                logger.debug("[Updates] listener failed", exc_info=True)

    def _set(self, state: str, code: str = "", detail: str = "") -> None:
        with self._lock:
            self.state, self.reason_code, self.detail = state, code, detail
        self._publish()

    def _progress(self, b: int, total: int) -> None:
        with self._lock:
            self.bytes, self.total = b, total
        self._publish()

    def _busy(self) -> bool:
        return self.state in ("downloading", "verifying", "starting")

    def _save(self, patch: dict) -> None:
        from core import settings as settings_mod
        settings_mod.apply_patch(self.ctx.cfg, {"updates": patch})
        try:
            self.ctx.save_settings()
        except Exception:                    # noqa: BLE001
            logger.warning("[Updates] could not save settings", exc_info=True)

    # -- check ------------------------------------------------------------------------------------------------
    def check(self, reason: str = "manual") -> dict:
        u = self.ctx.cfg.updates
        if not u.repo:
            self._set("disabled", "disabled")
            return self.view()
        if not self._check_lock.acquire(blocking=False):
            return self.view()                 # one already runs: its result arrives on SSE
        try:
            if self._busy():
                return self.view()
            key = (u.repo, u.channel)
            if self.release_key and self.release_key != key:
                with self._lock:                 # another source: forget what the old one offered
                    self.release, self.release_key, self.dismissed = None, (), ""
                self._save({"latest": ""})
            self._set("checking")
            self.last_attempt = time.time()
            sess = None
            try:
                sess = self.session_factory()
                rel = fetch_release(sess, u.repo, u.channel)
            except CheckError as e:
                self.last_failed = True
                logger.info("[Updates] could not check repo=%s why=%s detail=%s via=%s", _ascii(u.repo, 120), e.code,
                            _ascii(e.detail, 160), reason)
                self._set("could_not_check", e.code, e.detail)
                return self.view()
            except Exception as e:             # noqa: BLE001 -- never an error popup: a quiet state
                self.last_failed = True
                logger.info("[Updates] could not check repo=%s err=%s via=%s", _ascii(u.repo, 120), _ascii(e), reason)
                self._set("could_not_check", "bad_response", str(e))
                return self.view()
            finally:
                if sess is not None:
                    try:
                        sess.close()
                    except Exception:          # noqa: BLE001
                        pass
            self.last_failed = False
            with self._lock:
                self.release, self.release_key = rel, key
                self.dismissed = ""            # "Later" lasts until this check
                self.bytes = self.total = 0
            self._save({"last_check": _now_iso(), "latest": rel["tag"]})
            newer = is_newer(rel["version"], paths.APP_VERSION)
            logger.info("[Updates] checked repo=%s latest=%s current=%s newer=%s checksum=%s installer=%s via=%s",
                        _ascii(u.repo, 120), _ascii(rel["version"], 40), paths.APP_VERSION, newer,
                        rel["checksum"] or "-", "yes" if rel["asset"] else "no", reason)
            self._set("available" if newer else "up_to_date")
            return self.view()
        finally:
            self._check_lock.release()

    def later(self) -> dict:
        with self._lock:
            self.dismissed = (self.release or {}).get("version") or ""
        logger.info("[Updates] later version=%s", _ascii(self.dismissed, 40) or "-")
        self._publish()
        return self.view()

    def skip(self, version: str = "") -> dict:
        ver = clean_version(version) or (self.release or {}).get("version") or ""
        if ver:
            self._save({"skipped_version": ver})
            logger.info("[Updates] skip version=%s", _ascii(ver, 40))
        self._publish()
        return self.view()

    # -- automatic loop ------------------------------------------------------------------------------------------
    def start(self, delay: float = STARTUP_DELAY_S) -> bool:
        """The automatic check thread. Server runs only (main.py); never with WARDOGCLIPS_NO_UPDATE_CHECK set."""
        if auto_disabled():
            logger.info("[Updates] automatic check off (environment)")
            return False
        if self.thread is not None and self.thread.is_alive():
            return True
        try:
            sweep()
        except Exception:                      # noqa: BLE001
            logger.debug("[Updates] sweep failed", exc_info=True)
        self._stop.clear()
        self.thread = threading.Thread(target=self._loop, args=(delay,), name="update-check", daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        job = self._job
        if job is not None:
            job.cancel.set()

    def _loop(self, delay: float) -> None:
        if self._stop.wait(delay):
            return
        first = True
        while not self._stop.is_set():
            u = self.ctx.cfg.updates
            try:
                if u.auto_check and u.repo and (first or is_due(u, time.time(), self.last_attempt, self.last_failed)):
                    self.check("auto")
                first = False if (u.auto_check and u.repo) else first
            except Exception:                  # noqa: BLE001 -- the loop must survive anything
                logger.warning("[Updates] automatic check failed", exc_info=True)
            self._wake.wait(TICK_S)
            self._wake.clear()

    # -- download + hand-off ------------------------------------------------------------------------------------
    def install(self, confirm_unverified: bool = False) -> dict:
        """'Update now'. Returns {"ok", "code", "reason", ...view}. Never raises for a refusal."""
        with self._lock:
            rel = self.release
            if self._busy():
                return self._refusal("busy")
            if not rel or not is_newer(rel["version"], paths.APP_VERSION):
                return self._refusal("nothing")
            if not paths.INSTALLED:
                return self._refusal("not_installed")
            asset = rel.get("asset") or {}
            if not asset.get("url"):
                return self._refusal("no_asset")
            if asset.get("size", 0) > INSTALLER_MAX:
                return self._refusal("too_big")
            if not rel.get("checksum") and not confirm_unverified:
                return self._refusal("need_confirm")
            self._job = job = _DlJob(self)
            job.total = int(asset.get("size") or 0)
            self.bytes, self.total = 0, job.total
            self.state, self.reason_code = "downloading", ""
        self._publish()
        logger.info("[Updates] update now version=%s checksum=%s", _ascii(rel["version"], 40), rel["checksum"] or "none")
        self._dl_thread = threading.Thread(target=self._download, args=(rel, job), name="update-download", daemon=True)
        self._dl_thread.start()
        out = self.view()
        out.update(ok=True, code="started")
        return out

    def _refusal(self, code: str) -> dict:
        out = self.view()
        out.update(ok=False, code=code, reason=MSG.get(code, code), reason_i18n=reason_i18n(code))
        return out

    def cancel(self) -> bool:
        job = self._job
        if job is None or self.state != "downloading":
            return False
        job.cancel.set()
        return True

    def _expected_sha(self, sess, rel: dict) -> str:
        if rel.get("checksum") != "file":
            return rel.get("sha256") or ""
        try:
            r = _get(sess, rel["sha256_url"], stream=True)
            try:
                if r.status_code != 200:
                    raise CheckError("checksum_unreadable", f"HTTP {r.status_code}")
                raw = b""
                for chunk in r.iter_content(1024):
                    raw += chunk or b""
                    if len(raw) > SHA_FILE_MAX:
                        raise CheckError("checksum_unreadable", "checksum file too large")
            finally:
                r.close()
        except CheckError:
            raise
        except Exception as e:                 # noqa: BLE001
            raise CheckError("checksum_unreadable", str(e)) from e
        text = raw.decode("ascii", "replace")
        m = SHA_RE.search(text)
        if not m:
            raise CheckError("checksum_unreadable", "no sha256 in the checksum file")
        return m.group(0).lower()

    def _download(self, rel: dict, job: _DlJob) -> None:
        from core import packs
        asset = rel["asset"]
        name = INSTALLER_FMT.format(ver=rel["version"])
        d = updates_dir()
        final = d / name
        part = d / (name + ".part")
        sess = None
        try:
            d.mkdir(parents=True, exist_ok=True)
            sess = self.session_factory()
            want = self._expected_sha(sess, rel)
            if final.exists():
                final.unlink()                 # never trust a file left from before: fetch + verify again
            digest = packs._stream(job, sess, asset["url"], part, int(asset.get("size") or 0), hashlib.sha256, 0, name)
            self._set("verifying")
            if want and digest.lower() != want:
                part.unlink(missing_ok=True)   # a corrupt part must not be resumed
                logger.warning("[Updates] checksum mismatch file=%s want=%s got=%s", name, want, digest)
                self._set("failed", "bad_checksum")
                return
            os.replace(part, final)
            logger.info("[Updates] downloaded file=%s bytes=%d sha256=%s verified=%s", name, final.stat().st_size,
                        digest, bool(want))
        except packs._Cancelled:
            logger.info("[Updates] download canceled file=%s", name)
            self._set("available", "cancelled")
            return
        except CheckError as e:
            if e.code == "too_big":
                part.unlink(missing_ok=True)
            logger.warning("[Updates] download refused why=%s detail=%s", e.code, _ascii(e.detail))
            self._set("failed", e.code, e.detail)
            return
        except Exception as e:                 # noqa: BLE001 -- never raises out of the thread
            logger.warning("[Updates] download failed file=%s err=%s", name, _ascii(e))
            self._set("failed", "download_failed", str(e))
            return
        finally:
            if sess is not None:
                try:
                    sess.close()
                except Exception:              # noqa: BLE001
                    pass
            self._job = None
        if job.cancel.is_set():
            self._set("available", "cancelled")
            return
        self.handoff(final)

    def handoff(self, installer: Path) -> bool:
        """Start the installer (its normal wizard, visible) and shut this app down cleanly right after."""
        self._set("starting")
        try:
            launch_installer(installer)
        except Exception as e:                 # noqa: BLE001
            logger.warning("[Updates] installer did not start file=%s err=%s", installer.name, _ascii(e))
            self._set("failed", "launch_failed", str(e))
            return False
        self.launched = str(installer)
        logger.info("[Updates] installer started file=%s -- shutting down for the update", installer.name)
        cb = getattr(self.ctx, "request_quit", None)
        if cb is None:
            logger.info("[Updates] no quit hook (tests) -- not shutting down")
            return True

        def _later():
            time.sleep(HANDOFF_DELAY_S)
            try:
                cb()
            except Exception:                  # noqa: BLE001
                logger.exception("[Updates] quit after the hand-off failed")
        threading.Thread(target=_later, name="update-quit", daemon=True).start()
        return True


def launch_installer(path: Path) -> None:
    """The installer runs detached from this process (it must outlive our shutdown) with its normal UI; a build that
    needs elevation goes through ShellExecute (the UAC prompt) instead."""
    p = Path(path)
    if not p.is_file() or p.suffix.lower() != ".exe" or Path(updates_dir()).resolve() not in p.resolve().parents:
        raise RuntimeError("not an installer in the updates folder")
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    try:
        subprocess.Popen([str(p)], cwd=str(p.parent), close_fds=True, creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        if getattr(e, "winerror", None) == 740:            # ERROR_ELEVATION_REQUIRED
            os.startfile(str(p))                           # noqa: S606 -- our verified installer
        else:
            raise


def sweep(max_part_age_s: float = PART_MAX_AGE_S) -> int:
    """Startup clean-up of <data dir>\\updates: installers of this version or older (the update happened) and stale
    partial downloads. Never anything else."""
    d = updates_dir()
    if not d.is_dir():
        return 0
    n = 0
    now = time.time()
    for f in d.iterdir():
        try:
            m = re.fullmatch(r"WardogClips-Setup-(.+)\.exe(\.part)?", f.name, flags=re.I)
            if not m or not f.is_file():
                continue
            old = not is_newer(m.group(1), paths.APP_VERSION)
            stale = bool(m.group(2)) and now - f.stat().st_mtime > max_part_age_s
            if old or stale:
                f.unlink()
                n += 1
        except OSError:
            continue
    if n:
        logger.info("[Updates] removed old update files n=%d", n)
    return n
