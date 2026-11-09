"""Translatable texts that core code hands to the page (see utils/i18n.py msg / with_msg / app_error).

- MsgError: an exception whose str() is the English text and which carries the catalog key + params, so a route can
  turn it into a translatable HTTP error (http_error) and old callers that catch ValueError / RuntimeError and show
  str(e) keep working.
- Texts kept as plain English (a job's progress message or error in the database, an add-on's state, a stage note)
  are matched back to their catalog key when they are sent to the page: text_i18n / annotate. Every "server." key
  with enough fixed text takes part, so such a text must be written with msg(...)["message"] (the English then equals
  the catalog text, which a test checks).
"""

from __future__ import annotations

import re

from fastapi import HTTPException

from utils import i18n
from utils.i18n import AppError, msg

__all__ = ["MsgError", "http_error", "text_i18n", "annotate", "annotate_all", "msg"]

PREFIX = "server."
MIN_FIXED = 4                     # a template needs this much fixed text to be matched back ("{language}" never is)
_ERR_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception): ")


class MsgError(ValueError, RuntimeError):
    """An error with a translatable text: raise MsgError(msg("server.x", "English {a}", a=1)). str(e) is the English
    text; e.code / e.params are the catalog key and its params. It is a ValueError and a RuntimeError, so existing
    `except ValueError` / `except RuntimeError` handlers still catch it."""

    def __init__(self, m: dict):
        super().__init__(m["message"])
        self.code = m["code"]
        self.params = dict(m.get("params") or {})
        self.i18n = {"code": self.code, "params": self.params}


def http_error(status: int, exc: BaseException) -> HTTPException:
    """The HTTP error for a caught exception: translatable when it is a MsgError (or its text is a catalog text),
    else the plain English text."""
    code = getattr(exc, "code", None)
    params = getattr(exc, "params", None)
    if not (isinstance(code, str) and code.startswith("server.")):
        meta = text_i18n(str(exc))               # an English text written with msg() (e.g. a NotFound)
        code, params = (meta["code"], meta["params"]) if meta else (None, None)
    if code:
        err = AppError(status, code, str(exc))
        err.params = dict(params or {})
        return err
    return HTTPException(status, str(exc))


_cache: dict = {"sig": None, "pats": []}


def _patterns() -> list:
    cat = i18n.catalog(i18n.SOURCE)
    if _cache["sig"] is not cat:
        pats = []
        for key, text in cat.items():
            if not key.startswith(PREFIX):
                continue
            for form in (text.values() if isinstance(text, dict) else (text,)):
                pat = _compile(form)
                if pat is not None:
                    pats.append((key, pat))
        # the most specific (longest fixed text) template first
        pats.sort(key=lambda kp: -kp[1][1])
        _cache.update(sig=cat, pats=[(k, p[0]) for k, p in pats])
    return _cache["pats"]


def _compile(text):
    """(regex, fixed length) of one catalog text, None when it is not a string or has too little fixed text."""
    if not isinstance(text, str):
        return None
    names: list[str] = []
    parts = []
    pos = 0
    for m in i18n.PLACEHOLDER_RE.finditer(text):
        parts.append(re.escape(text[pos:m.start()]))
        if m.group(1) in names:
            parts.append(f"(?P={m.group(1)})")
        else:
            names.append(m.group(1))
            parts.append(f"(?P<{m.group(1)}>.+?)")
        pos = m.end()
    parts.append(re.escape(text[pos:]))
    fixed = len(i18n.PLACEHOLDER_RE.sub("", text).strip())
    if fixed < MIN_FIXED:
        return None
    return re.compile("".join(parts), re.S), fixed


def text_i18n(text, _depth: int = 0) -> dict | None:
    """{code, params} for an English text written by core code with msg() ("RuntimeError: " prefixes ignored), else
    None. Numeric params come back as numbers; a param that is itself such a text gets "<name>_i18n" beside it."""
    if not isinstance(text, str) or not text:
        return None
    s = _ERR_PREFIX.sub("", text, count=1)
    for key, pat in _patterns():
        m = pat.fullmatch(s)
        if m:
            params = {k: (int(v) if v.isdigit() else v) for k, v in m.groupdict().items()}
            if _depth < 1:
                for k, v in list(params.items()):
                    inner = text_i18n(v, _depth + 1) if isinstance(v, str) and len(v) >= MIN_FIXED else None
                    if inner:
                        params[k + "_i18n"] = inner
            return {"code": key, "params": params}
    return None


def annotate(row: dict | None, *fields: str) -> dict | None:
    """Add <field>_i18n {code, params} beside each English text field of a row the page shows. Returns row."""
    if not isinstance(row, dict):
        return row
    for f in fields or ("message", "error"):
        meta = text_i18n(row.get(f))
        if meta:
            row[f + "_i18n"] = meta
    return row


def annotate_all(rows, *fields: str):
    for r in rows or ():
        annotate(r, *fields)
    return rows
