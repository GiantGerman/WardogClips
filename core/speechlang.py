"""Speech languages: which languages speech-to-text listens for, and what that means for the model.

The setting (`stt.language`, and the per-recording override `recordings.stt_language`) is one string:
- "auto"     detect the language of every speech track (any language Whisper knows)
- "en"       one fixed language: no detection, every track is transcribed as that language
- "en,es"    a short list: each track's language is detected, but only among these (fewer wrong guesses on short
             or noisy audio than a free auto-detect)

The English-only models ("medium.en", "small.en", "tiny.en") cannot write anything but English, so as soon as any
other language is possible the multilingual model of the same size is used instead (`effective_model`). The saved
model choice stays the English one: switching back to English-only goes back to it.

Pure data + string handling: no faster-whisper import here (settings and the routes use it).
"""

from __future__ import annotations

# The languages of the Americas + the European Union that Whisper can transcribe, in the order the UI lists them:
# (code, English name, the language's own name). Regional variants (es-419 / es-ES, pt-BR / pt-PT, fr-CA / fr-FR)
# share one speech language.
LANGUAGES: tuple[tuple[str, str, str], ...] = (
    ("en", "English", "English"),
    ("es", "Spanish", "Español"),
    ("pt", "Portuguese", "Português"),
    ("fr", "French", "Français"),
    ("de", "German", "Deutsch"),
    ("it", "Italian", "Italiano"),
    ("pl", "Polish", "Polski"),
    ("nl", "Dutch", "Nederlands"),
    ("bg", "Bulgarian", "Български"),
    ("hr", "Croatian", "Hrvatski"),
    ("cs", "Czech", "Čeština"),
    ("da", "Danish", "Dansk"),
    ("et", "Estonian", "Eesti"),
    ("fi", "Finnish", "Suomi"),
    ("el", "Greek", "Ελληνικά"),
    ("hu", "Hungarian", "Magyar"),
    ("lv", "Latvian", "Latviešu"),
    ("lt", "Lithuanian", "Lietuvių"),
    ("mt", "Maltese", "Malti"),
    ("ro", "Romanian", "Română"),
    ("sk", "Slovak", "Slovenčina"),
    ("sl", "Slovenian", "Slovenščina"),
    ("sv", "Swedish", "Svenska"),
    ("ht", "Haitian Creole", "Kreyòl ayisyen"),
)
CODES = tuple(c for c, _n, _o in LANGUAGES)
NAMES = {c: n for c, n, _o in LANGUAGES}

# Languages of the same regions that Whisper has no model for: shown in the UI as "not available", so the user knows
# to pick auto-detect (or the closest language) for them.
UNSUPPORTED: dict[str, str] = {"ga": "Irish", "qu": "Quechua", "gn": "Guarani", "ay": "Aymara"}

DEFAULT = "en"
AUTO = "auto"

# English-only model -> the multilingual model of the same size (large-v3 / large-v3-turbo are multilingual already)
MULTILINGUAL_OF: dict[str, str] = {"tiny.en": "tiny", "small.en": "small", "medium.en": "medium"}

# Excitement / highlight keywords per language: short things gamers shout. Matched as whole words, case and
# apostrophes ignored (core/highlights.py), so "lets go" finds "Let's go!".
DEFAULT_KEYWORDS: dict[str, list[str]] = {
    "en": ["let's go", "nice shot", "got him", "holy", "oh my god", "no way", "are you kidding"],
    "es": ["vamos", "qué tiro", "lo tengo", "lo maté", "buen tiro", "no puede ser", "dios mío"],
    "pt": ["vamos", "que tiro", "peguei", "matei", "boa", "não acredito", "meu deus"],
    "fr": ["allez", "quel tir", "je l'ai eu", "bien joué", "c'est pas possible", "oh mon dieu"],
    "de": ["los geht's", "hab ihn", "guter schuss", "krass", "oh mein gott", "nicht dein ernst"],
    "it": ["andiamo", "che colpo", "preso", "bel colpo", "non ci credo", "oddio"],
    "pl": ["dawaj", "mam go", "dobry strzał", "o boże", "nie wierzę", "jazda"],
    "nl": ["kom op", "mooi schot", "heb hem", "wauw","oh mijn god", "no way"],
}


def _codes_from(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        parts = [str(x) for x in value]
    else:
        parts = str(value or "").replace(";", ",").replace(" ", ",").split(",")
    out: list[str] = []
    for p in parts:
        c = p.strip().lower().replace("_", "-")
        if not c:
            continue
        if c == AUTO:
            return [AUTO]
        c = c.split("-", 1)[0]                     # pt-BR -> pt, es-419 -> es
        if c in CODES and c not in out:
            out.append(c)
    return out


def normalize(value) -> str:
    """Any accepted form ("auto", "es", "en, es", ["en", "es"], "pt-BR") -> the canonical setting string. Unknown
    and unsupported codes are dropped; nothing left -> the default (English)."""
    codes = _codes_from(value)
    if codes == [AUTO]:
        return AUTO
    return ",".join(codes) if codes else DEFAULT


def is_known(value) -> bool:
    """True when the value names "auto" or at least one language from the list (unknown codes beside it are
    dropped by normalize)."""
    return bool(_codes_from(value))


def parse(value) -> dict:
    """{"mode": "auto" | "fixed" | "list", "codes": [...]} for a setting string."""
    v = normalize(value)
    if v == AUTO:
        return {"mode": "auto", "codes": []}
    codes = v.split(",")
    return {"mode": "fixed" if len(codes) == 1 else "list", "codes": codes}


def needs_multilingual(value) -> bool:
    """Anything but plain English needs a multilingual model."""
    return normalize(value) != "en"


def effective_model(model: str, value) -> str:
    """The model really loaded for this language setting: an English-only model is swapped for its multilingual
    twin when any other language is possible."""
    if needs_multilingual(value):
        return MULTILINGUAL_OF.get(model, model)
    return model


def for_recording(cfg, rec: dict | None) -> str:
    """The language setting that applies to one recording: its own override, else Settings."""
    own = (rec or {}).get("stt_language")
    return normalize(own) if own else normalize(getattr(cfg.stt, "language", DEFAULT))


def default_speech_language(ui_lang: str | None) -> str:
    """The speech language a fresh install starts with, from the UI language ("de-DE" -> "de", "pt-BR" -> "pt");
    English when the UI language is English, unknown, or one Whisper cannot transcribe (Irish, Quechua, ...)."""
    codes = _codes_from(ui_lang or "")
    return codes[0] if codes and codes[0] != AUTO else DEFAULT


def name_of(code: str | None) -> str:
    if not code:
        return ""
    return NAMES.get(code) or UNSUPPORTED.get(code) or code


def describe(value) -> str:
    """Plain words for a setting, for job notes and the UI (English; describe_msg has the translatable form)."""
    return describe_msg(value)["message"]


def describe_msg(value) -> dict:
    """describe() as a translatable server message (utils/i18n.msg): params carry the language codes too, so the page
    can name the languages in the UI language."""
    from utils.i18n import msg
    p = parse(value)
    if p["mode"] == "auto":
        return msg("server.speech.describe_auto", "Detect automatically")
    if p["mode"] == "fixed":
        return msg("server.speech.describe_one", "{language}", language=name_of(p["codes"][0]), codes=p["codes"][0])
    return msg("server.speech.describe_list", "Detect among {languages}",
               languages=", ".join(name_of(c) for c in p["codes"]), codes=",".join(p["codes"]))


def default_keywords() -> dict[str, list[str]]:
    return {k: list(v) for k, v in DEFAULT_KEYWORDS.items()}


def clean_keyword_map(value) -> dict[str, list[str]]:
    """{lang: [phrases]} with lower-case codes and phrases, empty phrases and duplicates dropped. A plain string /
    list (the old single English list) becomes the "en" entry."""
    if isinstance(value, (str, list, tuple)):
        value = {"en": value}
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[str]] = {}
    for k, v in value.items():
        code = str(k or "").strip().lower().split("-", 1)[0]
        if not code or len(code) > 3 or not code.isalpha():
            continue
        if isinstance(v, str):
            items = v.replace("\n", ";").split(";")
        elif isinstance(v, (list, tuple)):
            items = [str(x) for x in v]
        else:
            continue
        seen: list[str] = []
        for it in items:
            t = it.strip().lower()
            if t and t not in seen:
                seen.append(t)
        out[code] = seen
    return out


def catalog() -> dict:
    """What the page needs to draw the language pickers."""
    return {"languages": [{"code": c, "name": n, "native": o} for c, n, o in LANGUAGES],
            "unsupported": [{"code": c, "name": n} for c, n in UNSUPPORTED.items()],
            "multilingual_of": dict(MULTILINGUAL_OF), "default": DEFAULT}
