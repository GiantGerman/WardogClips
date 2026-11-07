"""Resume points for long stages: detect's keyframe pass and transcribe's whisper pass save what they
have so far every CHECKPOINT_EVERY_S, and a re-run of the same stage on the same recording (the app was closed, the
job was stopped, the PC went down) picks up there instead of from 0 %.

One file per (stage, recording) under `<DATA_DIR>/checkpoints/` (`detect_rec<id>.pkl`). It holds a fingerprint of
everything the result depends on (the file's size + mtime, the boxes, the rules, the model...); a checkpoint whose
fingerprint differs is thrown away and the stage starts over. Written atomically (tmp + os.replace), so a crash in
the middle of a save leaves the previous checkpoint. The stage clears its checkpoint once its result is written.

Pickle is fine here: these files are written only by this app, into its own data dir, and read back by it.
"""

import hashlib
import logging
import os
import pickle
import time
from pathlib import Path

from utils import paths

logger = logging.getLogger("wclips.checkpoints")

VERSION = 1
CHECKPOINT_EVERY_S = 60.0          # wall time between saves
MAX_AGE_DAYS = 14.0                # sweep() drops older ones (and the transcribe wavs they kept)


def _dir() -> Path:
    d = paths.DATA_DIR / "checkpoints"
    d.mkdir(parents=True, exist_ok=True)
    return d


def path_for(stage: str, rec_id: int) -> Path:
    return _dir() / f"{stage}_rec{int(rec_id)}.pkl"


def file_stamp(path: str) -> list:
    """[size, mtime_ns] of the footage (a stat only: the footage is never opened for writing)."""
    st = os.stat(path)
    return [st.st_size, st.st_mtime_ns]


_STAMPS: dict[tuple, str] = {}


def code_stamp(*rel_paths: str) -> str:
    """sha1 of these source files (relative to the project root), read once per process: a checkpoint made by other
    code is not resumed (the part already done would carry the old rules / labels)."""
    if rel_paths not in _STAMPS:
        h = hashlib.sha1()
        for rel in rel_paths:
            try:
                h.update((paths.PROJECT_ROOT / rel).read_bytes())
            except OSError:
                h.update(rel.encode())
        _STAMPS[rel_paths] = h.hexdigest()[:16]
    return _STAMPS[rel_paths]


def load(stage: str, rec_id: int, fingerprint: dict) -> dict | None:
    """The saved state when its fingerprint matches, else None (a stale checkpoint is removed)."""
    p = path_for(stage, rec_id)
    if not p.exists():
        return None
    try:
        with open(p, "rb") as f:
            data = pickle.load(f)
    except Exception as e:  # noqa: BLE001 - a broken checkpoint only costs the head start
        logger.warning("[Checkpoint] unreadable %s: %s -- starting over", p.name, e)
        clear(stage, rec_id)
        return None
    if not isinstance(data, dict) or data.get("v") != VERSION or data.get("fp") != fingerprint:
        logger.info("[Checkpoint] %s rec=%s no longer matches (file / settings changed) -- starting over", stage, rec_id)
        clear(stage, rec_id, keep_files=False, state=data if isinstance(data, dict) else None)
        return None
    return data.get("state")


def save(stage: str, rec_id: int, fingerprint: dict, state: dict) -> bool:
    p = path_for(stage, rec_id)
    tmp = p.with_suffix(".tmp")
    try:
        with open(tmp, "wb") as f:
            pickle.dump({"v": VERSION, "fp": fingerprint, "saved_utc": time.time(), "state": state}, f,
                        protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, p)
        return True
    except OSError as e:
        logger.warning("[Checkpoint] save failed %s: %s", p.name, e)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def clear(stage: str, rec_id: int, keep_files: bool = False, state: dict | None = None) -> None:
    """Remove the checkpoint; with keep_files False also the side files it names (`state["files"]`, e.g. the
    transcribe wavs). `state` = an already loaded state (else it is read from disk for its file list)."""
    p = path_for(stage, rec_id)
    if not keep_files:
        if state is None and p.exists():
            try:
                with open(p, "rb") as f:
                    state = pickle.load(f)
            except Exception:  # noqa: BLE001
                state = None
        files = []
        if isinstance(state, dict):
            files = (state.get("state") or state).get("files") or []
        for fp in files:
            _unlink_cache(fp)
    try:
        p.unlink()
    except OSError:
        pass


def _unlink_cache(fp) -> None:
    """Only files inside the cache dir are ever removed (never footage)."""
    try:
        f = Path(fp).resolve()
        if paths.CACHE_DIR.resolve() in f.parents:
            f.unlink()
    except OSError:
        pass


def sweep(max_age_days: float = MAX_AGE_DAYS, keep: set | None = None) -> int:
    """Drop checkpoints nobody resumed for max_age_days (with their cache files). `keep` = {(stage, rec_id)} of jobs
    still queued / running: those wait for their job however old (the app may simply have been closed for weeks)."""
    n = 0
    cutoff = time.time() - max_age_days * 86400
    for p in _dir().glob("*"):
        try:
            if p.stat().st_mtime >= cutoff:
                continue
            if p.suffix == ".pkl" and "_rec" in p.stem:
                stage, rid = p.stem.rsplit("_rec", 1)
                if keep and (stage, int(rid)) in keep:
                    continue
                clear(stage, int(rid))
            else:
                p.unlink()
            n += 1
        except (OSError, ValueError):
            continue
    if n:
        logger.info("[Checkpoint] removed %d stale checkpoint(s)", n)
    return n
