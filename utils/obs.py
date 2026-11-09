"""OBS Studio's own description of the recording's audio tracks, read-only.

OBS keeps the track names in the active profile's basic.ini ([AdvOut] Track1Name..Track6Name);
the active profile is global.ini [Basic] Profile / ProfileDir. Only these two text files under
%APPDATA%\\obs-studio are read -- never the game, never Steam, never OBS's process.
"""

import configparser
import logging
import os
from pathlib import Path

logger = logging.getLogger("wclips.obs")

N_TRACKS = 6


def obs_dir() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    d = Path(appdata) / "obs-studio"
    return d if d.is_dir() else None


def _ini(path: Path) -> configparser.ConfigParser | None:
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            cp.read(path, encoding=enc)
            return cp
        except (UnicodeError, configparser.Error):
            continue
    return None


def active_profile_dir(base: Path | None = None) -> Path | None:
    base = base or obs_dir()
    if base is None:
        return None
    profiles = base / "basic" / "profiles"
    g = _ini(base / "global.ini") if (base / "global.ini").exists() else None
    name = None
    if g is not None:
        for sec in ("Basic", "General"):
            if g.has_section(sec):
                name = g.get(sec, "ProfileDir", fallback=None) or g.get(sec, "Profile", fallback=None)
                if name:
                    break
    if name and (profiles / name / "basic.ini").exists():
        return profiles / name
    found = sorted(profiles.glob("*/basic.ini"), key=lambda p: p.stat().st_mtime, reverse=True) if profiles.is_dir() else []
    return found[0].parent if found else None


def track_names(base: Path | None = None) -> list[str]:
    """['Game', 'Microphone', 'Discord', ...] for tracks 1..6, or [] when OBS is not set up here."""
    prof = active_profile_dir(base)
    if prof is None:
        return []
    cp = _ini(prof / "basic.ini")
    if cp is None:
        return []
    names = []
    for i in range(1, N_TRACKS + 1):
        v = ""
        for sec in ("AdvOut", "SimpleOutput"):
            if cp.has_section(sec) and cp.get(sec, f"Track{i}Name", fallback=""):
                v = cp.get(sec, f"Track{i}Name").strip()
                break
        names.append(v)
    if not any(names):
        return []
    logger.info("[OBS] track names from profile %s: %s", prof.name, ", ".join(f"{i + 1}={n or '-'}" for i, n in enumerate(names)))
    return names


def recording_paths(base: Path | None = None) -> list[str]:
    """The folder(s) the active OBS profile records to (the replay buffer saves into the same one): [AdvOut]
    RecFilePath, [SimpleOutput] FilePath, and [AdvOut] FFFilePath when RecType is FFmpeg. The output mode in use
    ([Output] Mode) comes first. Only folders that exist, de-duplicated (case-insensitive, normalized). The setup
    wizard proposes these as library roots; nothing is scanned until the user confirms."""
    prof = active_profile_dir(base)
    if prof is None:
        return []
    cp = _ini(prof / "basic.ini")
    if cp is None:
        return []

    def get(sec: str, key: str) -> str:
        return cp.get(sec, key, fallback="").strip() if cp.has_section(sec) else ""

    adv = [get("AdvOut", "RecFilePath")]
    if get("AdvOut", "RecType").lower() == "ffmpeg":
        adv.insert(0, get("AdvOut", "FFFilePath"))
    simple = [get("SimpleOutput", "FilePath")]
    mode = get("Output", "Mode").lower()
    order = adv + simple if mode.startswith("adv") else simple + adv
    out, seen = [], set()
    for p in order:
        if not p:
            continue
        p = os.path.normpath(os.path.expandvars(p.strip('"')))
        key = os.path.normcase(p).rstrip("/\\")
        if key in seen or not os.path.isdir(p):
            continue
        seen.add(key)
        out.append(p)
    if out:
        logger.info("[OBS] recording folders from profile %s: %s", prof.name, "; ".join(out))
    return out
