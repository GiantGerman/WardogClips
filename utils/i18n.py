"""User-facing language: the language registry, which language the UI uses, the catalogs the page loads, server
messages the page translates, and the small string tables of the tray icon.

Logs stay English and ASCII (they are read for bug reports); only text a user sees is translated.

- Registry: ``web/i18n/languages.json`` (code, English name, native name, speech language, beta flag, plural forms).
- Web catalogs: ``web/i18n/<code>/<namespace>.json``, flat ``{"key": "text" | {plural forms}}``; the full key the page
  asks for is ``<namespace>.<key>``. Keys starting with "@" are notes for translators and are never shown.
- Fallback chain: the exact language (pt-BR) -> its base language when a catalog folder exists for it (pt) -> en-US
  -> the key itself.
- The UI language: ``ui.language`` in settings; "" = the language the installer was run in (``install.ini`` next to
  the app), else the Windows display language, mapped to the nearest supported code.
- Server messages: ``msg()`` / ``app_error()`` / ``with_msg()`` carry the English text AND a catalog key + params,
  so the page shows the translation when it has one and the English text otherwise.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from functools import lru_cache
from typing import Any

from utils import paths

logger = logging.getLogger("wclips.i18n")

SOURCE = "en-US"                                  # the language every translation is made from
CATALOG_DIR = paths.WEB_DIR / "i18n"
LANGS_FILE = CATALOG_DIR / "languages.json"
DESKTOP_DIR = paths.PROJECT_ROOT / "desktop" / "i18n"
INSTALL_INI = paths.INSTALL_ROOT / "install.ini"  # written by the installer: [WardogClips] language=<code>
PLURAL_CATEGORIES = ("zero", "one", "two", "few", "many", "other")
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
ENV_SYSTEM_LANGUAGE = "WARDOGCLIPS_SYSTEM_LANGUAGE"   # dev / tests: pretend Windows runs in this language

# Windows names some languages with codes of their own: Quechua = quz (Bolivia / Ecuador / Peru)
_ALIASES = {"quz": "qu", "qub": "qu", "quh": "qu", "qvi": "qu", "nb": "", "nn": "", "no": ""}


# ---- registry -----------------------------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _registry() -> dict:
    try:
        return json.loads(LANGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("[I18n] language list unreadable path=%s err=%s", LANGS_FILE, e)
        return {"source": SOURCE, "languages": [{"code": SOURCE, "english": "English (United States)",
                                                 "native": "English (US)", "speech": "en", "speech_supported": True,
                                                 "beta": False, "plurals": ["one", "other"]}]}


def languages() -> list[dict]:
    """Every supported language, in menu order (copies: callers may change them)."""
    return [dict(x) for x in _registry().get("languages") or []]


def codes() -> list[str]:
    return [x["code"] for x in languages()]


def language(code: str) -> dict | None:
    for x in languages():
        if x["code"] == code:
            return x
    return None


def canonical(code: str | None) -> str:
    """'PT-br' -> 'pt-BR' when that code is supported; else ''."""
    c = str(code or "").strip().replace("_", "-").lower()
    for k in codes():
        if k.lower() == c:
            return k
    return ""


def nearest(tag: str | None) -> str:
    """The supported language closest to a BCP 47 / Windows locale name ("de-AT" -> "de", "es-MX" -> "es-419",
    "pt-AO" -> "pt-PT", "fr-BE" -> "fr-FR", "en-GB" -> "en-US"); en-US when nothing fits."""
    exact = canonical(tag)
    if exact:
        return exact
    t = str(tag or "").strip().replace("_", "-").lower()
    if not t:
        return SOURCE
    parts = t.split("-")
    base = _ALIASES.get(parts[0], parts[0])
    # the region is the first 2-letter or 3-digit subtag ("zh-Hant-TW": skip the 4-letter script)
    region = next((p for p in parts[1:] if (len(p) == 2 and p.isalpha()) or (len(p) == 3 and p.isdigit())), "")
    known = set(codes())
    if base == "es":
        pick = "es-ES" if region == "es" else "es-419"
    elif base == "pt":
        pick = "pt-PT" if region and region != "br" else "pt-BR"
    elif base == "fr":
        pick = "fr-CA" if region == "ca" else "fr-FR"
    elif base == "en":
        pick = SOURCE
    else:
        pick = canonical(base)
    return pick if pick in known else SOURCE


def normalize_setting(value) -> str:
    """The stored ui.language: "" (follow the installer / Windows) or a supported code ("de-DE" -> "de"); a language
    we do not have ("ja") -> ""."""
    v = str(value or "").strip()
    if not v:
        return ""
    exact = canonical(v)
    if exact:
        return exact
    near = nearest(v)
    return near if near != SOURCE or base_of(v) == "en" else ""


# ---- which language is in use ---------------------------------------------------------------------------------------

def windows_ui_language() -> str:
    """The Windows display language as a locale name ("de-DE"), "" when unknown. Reads the user's UI language
    (GetUserDefaultUILanguage + LCIDToLocaleName); WARDOGCLIPS_SYSTEM_LANGUAGE overrides it for tests."""
    env = os.environ.get(ENV_SYSTEM_LANGUAGE, "").strip()
    if env:
        return env
    if os.name != "nt":
        return ""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        langid = int(k32.GetUserDefaultUILanguage())
        buf = ctypes.create_unicode_buffer(85)
        if k32.LCIDToLocaleName(langid, buf, 85, 0) > 0 and buf.value:
            return buf.value
        if k32.GetUserDefaultLocaleName(buf, 85) > 0:
            return buf.value
    except (AttributeError, OSError, ValueError) as e:
        logger.debug("[I18n] windows language not read err=%s", e)
    return ""


def installer_language() -> str:
    """The language the installer ran in (install.ini next to the app), "" for a source run or none written."""
    try:
        text = INSTALL_INI.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        try:
            text = INSTALL_INI.read_text(encoding="utf-16", errors="replace")
        except OSError:
            return ""
    m = re.search(r"^\s*language\s*=\s*([A-Za-z0-9_-]+)\s*$", text, re.M | re.I)
    return canonical(m.group(1)) if m else ""


def system_language() -> str:
    """The default when ui.language is "": the installer's choice, else Windows' display language."""
    return installer_language() or nearest(windows_ui_language())


def active_language(cfg=None) -> str:
    """The language the UI uses right now for this settings object."""
    setting = canonical(getattr(getattr(cfg, "ui", None), "language", "") or "")
    return setting or system_language()


def base_of(code: str) -> str:
    return str(code or "").split("-", 1)[0].lower()


def chain(code: str) -> list[str]:
    """Lookup order for one language: exact -> base (only when web/i18n/<base>/ exists) -> en-US."""
    out = [code] if code else []
    b = base_of(code)
    if b and b != code and (CATALOG_DIR / b).is_dir():
        out.append(b)
    if SOURCE not in out:
        out.append(SOURCE)
    return out


# ---- web catalogs ---------------------------------------------------------------------------------------------------

def namespaces(code: str = SOURCE) -> list[str]:
    d = CATALOG_DIR / code
    return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []


def read_namespace(code: str, ns: str) -> dict:
    """One catalog file as a dict ({} when missing or broken -- logged once per read)."""
    f = CATALOG_DIR / code / f"{ns}.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning("[I18n] catalog unreadable path=%s err=%s", f, e)
        return {}
    return data if isinstance(data, dict) else {}


def _signature(code: str) -> tuple:
    d = CATALOG_DIR / code
    if not d.is_dir():
        return ()
    return tuple(sorted((p.name, p.stat().st_mtime_ns) for p in d.glob("*.json")))


_cat_lock = threading.Lock()
_cat_cache: dict[str, tuple[tuple, dict]] = {}


def catalog(code: str) -> dict[str, Any]:
    """All namespaces of one language as one flat map {"ns.key": value}; notes ("@...") left out. Re-read when a
    file changes (cheap stat per call)."""
    sig = _signature(code)
    with _cat_lock:
        hit = _cat_cache.get(code)
        if hit and hit[0] == sig:
            return hit[1]
    flat: dict[str, Any] = {}
    for name, _m in sig:
        ns = name[:-5]
        for k, v in read_namespace(code, ns).items():
            if not str(k).startswith("@"):
                flat[f"{ns}.{k}"] = v
    with _cat_lock:
        _cat_cache[code] = (sig, flat)
    return flat


def bundle(code: str | None = None, cfg=None) -> dict:
    """What the page loads before it renders: the chain's catalogs (exact, base, en-US), the registry, dev flag."""
    lang = canonical(code) or active_language(cfg)
    ch = chain(lang)
    return {"lang": lang, "chain": ch, "catalogs": {c: catalog(c) for c in ch}, "source": SOURCE,
            "languages": public_languages(), "dev": not paths.INSTALLED}


def coverage(code: str) -> float:
    """Share of the en-US keys this language has its own text for (0..1)."""
    src = catalog(SOURCE)
    if not src or code == SOURCE:
        return 1.0
    own = catalog(code)
    b = base_of(code)
    base = catalog(b) if b != code and (CATALOG_DIR / b).is_dir() else {}
    n = sum(1 for k in src if k in own or k in base)
    return round(n / len(src), 3)


def public_languages() -> list[dict]:
    return [{"code": x["code"], "english": x["english"], "native": x["native"], "beta": bool(x.get("beta")),
             "speech": x.get("speech"), "speech_supported": bool(x.get("speech_supported"))} for x in languages()]


# ---- text formatting + server messages -------------------------------------------------------------------------------

def fmt(text: str, params: dict | None = None) -> str:
    """'{name}' placeholders filled from params (the same syntax the page's t() uses); unknown ones stay as they are."""
    p = params or {}
    return PLACEHOLDER_RE.sub(lambda m: str(p[m.group(1)]) if m.group(1) in p else m.group(0), str(text))


def placeholders(text) -> set[str]:
    if isinstance(text, dict):
        out: set[str] = set()
        for v in text.values():
            out |= placeholders(v)
        return out
    return set(PLACEHOLDER_RE.findall(str(text)))


def msg(code: str, english: str, /, **params) -> dict:
    """A message for the page: {"message": English text, "code": "server.<key>", "params": {...}}. The page shows
    t(code, params) when its catalogs have the key, else the English message. `english` must equal the en-US
    catalog text of `code` (a test checks every literal call)."""
    return {"message": fmt(english, params), "code": code, "params": _jsonable(params)}


def with_msg(payload: dict, field: str, code: str, english: str, /, **params) -> dict:
    """Put a translatable text into a JSON payload without changing its shape: payload[field] = the English text
    (old readers keep working) and payload[field + "_i18n"] = {"code", "params"}. Returns payload."""
    payload[field] = fmt(english, params)
    payload[field + "_i18n"] = {"code": code, "params": nest(_jsonable(params))}
    return payload


def _jsonable(params: dict) -> dict:
    out = {}
    for k, v in (params or {}).items():
        if k.endswith("_i18n") and isinstance(v, dict):         # a nested server text's {code, params}
            out[k] = {"code": str(v.get("code") or ""), "params": _jsonable(v.get("params") or {})}
        else:
            out[k] = v if isinstance(v, (str, int, float, bool)) or v is None else str(v)
    return out


def nest(params: dict) -> dict:
    """Add "<name>_i18n" {code, params} beside every string param that is itself a server text (an error text written
    with msg() and passed on as {error}), so the page shows it translated inside the outer text. Returns params."""
    try:
        from core.messages import text_i18n        # late: core.messages imports this module
    except Exception:                              # noqa: BLE001 -- no nesting rather than no message
        return params
    for k, v in list(params.items()):
        if isinstance(v, str) and len(v) >= 4 and not k.endswith("_i18n") and (k + "_i18n") not in params:
            meta = text_i18n(v)
            if meta:
                params[k + "_i18n"] = meta
    return params


try:                                              # the app always has FastAPI; keep this module importable without
    from fastapi import HTTPException as _HTTPException
except ImportError:                               # pragma: no cover
    _HTTPException = Exception                    # type: ignore[misc,assignment]


class AppError(_HTTPException):                   # type: ignore[misc,valid-type]
    """An HTTP error whose text the page can translate. The response body is
    {"detail": English text, "code": "server.<key>", "params": {...}} (server/app.py registers the handler), so
    callers reading only "detail" keep working."""

    def __init__(self, status_code: int, code: str, english: str, /, **params):
        m = msg(code, english, **params)
        super().__init__(status_code=status_code, detail=m["message"])   # type: ignore[call-arg]
        self.code = code
        self.params = nest(m["params"])


def app_error(status_code: int, code: str, english: str, /, **params) -> AppError:
    """raise app_error(409, "server.vods_busy", "A download is already running for {site}", site=s)"""
    return AppError(status_code, code, english, **params)


# ---- server-side lookups (tray, native dialogs) ----------------------------------------------------------------------

def lookup(code: str, key: str):
    """The raw catalog value of a full web key for one language, following the chain; None when missing."""
    for c in chain(code):
        v = catalog(c).get(key)
        if v is not None:
            return v
    return None


def plural_pick(value, count, code: str = SOURCE) -> str:
    """Python has no CLDR rules: 'one' for 1 (and 0 in French / Portuguese), else 'other'. Good enough for the tray;
    the page uses Intl.PluralRules."""
    if not isinstance(value, dict):
        return str(value)
    try:
        n = float(count)
    except (TypeError, ValueError):
        n = 2
    one = n == 1 or (base_of(code) in ("fr", "pt") and n == 0)
    return str(value.get("one") if one and "one" in value else value.get("other", ""))


# ---- desktop (tray icon) string tables ------------------------------------------------------------------------------

def _desktop_table(code: str) -> dict:
    f = DESKTOP_DIR / f"{code}.json"
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if not str(k).startswith("@")} if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning("[I18n] desktop strings unreadable path=%s err=%s", f, e)
        return {}


def desktop_text(key: str, /, lang: str | None = None, english: str = "", **params) -> str:
    """A tray / native string: desktop/i18n/<lang>.json, then the base language, then en-US.json, then `english`,
    then the key. Placeholders as on the page."""
    for c in chain(lang or SOURCE) + [SOURCE]:
        v = _desktop_table(c).get(key)
        if v:
            return fmt(plural_pick(v, params.get("count"), c), params)
    return fmt(english or key, params)


# ---- language change listeners (tray menu) -----------------------------------------------------------------------------

_listeners: list = []


def on_change(fn) -> None:
    if fn not in _listeners:
        _listeners.append(fn)


def notify_change(code: str) -> None:
    logger.info("[I18n] ui language=%s", code)
    for fn in list(_listeners):
        try:
            fn(code)
        except Exception:                         # noqa: BLE001 -- a listener must never break a settings save
            logger.exception("[I18n] language listener failed")


def reset_caches() -> None:
    """Tests: forget the registry and catalogs read so far."""
    _registry.cache_clear()
    with _cat_lock:
        _cat_cache.clear()
