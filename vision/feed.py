"""The kill feed, top-left: rows `<killer> <weapon icon> <victim>`, with a distance tag
`[60 m]` on the victim side whenever the player is involved (either side). Names carry
clan tags (`[ABC] SquadMate`), unicode happens (Hangul names were seen), and OCR mangles a
letter now and then -- so matching your aliases is fuzzy.
"""

import difflib
import re
from dataclasses import dataclass, field

from vision.ocr import Box
from vision.ticker import group_rows

_DIST = re.compile(r"\[?\s*(\d{1,4})\s*[mM]\s*\]?")
_TAG = re.compile(r"^\s*\[[^\]]{1,6}\]\s*")


@dataclass
class Row:
    t_s: float
    killer: str
    victim: str
    distance_m: int | None
    raw: str
    conf: float = 0.0
    role: str = ""              # kill | death | squad_kill | squad_death | ''
    boxes: list = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{clean_name(self.killer)}>{clean_name(self.victim)}"


def clean_name(s: str) -> str:
    """Drop the clan tag, lower-case, strip spaces and punctuation OCR invents."""
    s = _TAG.sub("", s or "")
    s = re.sub(r"[^\w]", "", s.lower())
    return s


def name_matches(text: str, aliases: list[str], min_ratio: float = 0.75) -> bool:
    """Exact after cleaning, the alias inside the text, or (for names of 5+ letters) a
    fuzzy ratio -- OCR turns `SquadMate` into `SquadMat` and `SQuaDMATE`."""
    n = clean_name(text)
    if not n:
        return False
    for a in aliases:
        ca = clean_name(a)
        if not ca:
            continue
        if ca == n or (len(ca) >= 5 and ca in n):
            return True
        if len(ca) >= 5 and len(n) >= 4 and difflib.SequenceMatcher(None, ca, n).ratio() >= min_ratio:
            return True
    return False


def parse_row(row: list[Box], t_s: float) -> Row | None:
    """One visual row of boxes -> Row. The distance tag splits killer | victim; without one
    the widest horizontal gap (the weapon icon) does."""
    texts = [b.text for b in row]
    raw = " ".join(texts)
    dist = None
    split_at = None
    for i, b in enumerate(row):
        m = _DIST.search(b.text)
        if m and (b.text.strip().startswith("[") or b.text.strip().endswith("]") or len(b.text.strip()) <= 8):
            dist = int(m.group(1))
            split_at = i
            break
    if split_at is not None:
        left = row[:split_at]
        tagbox = row[split_at]
        rest = _DIST.sub("", tagbox.text).strip()
        right = ([rest] if rest else []) + [b.text for b in row[split_at + 1:]]
        killer = " ".join(b.text for b in left).strip()
        victim = " ".join(right).strip()
    else:
        if len(row) < 2:
            return None
        gaps = [(row[i + 1].x0 - row[i].x1, i) for i in range(len(row) - 1)]
        gap, i = max(gaps)
        if gap < row[0].h * 0.8:
            return None
        killer = " ".join(b.text for b in row[:i + 1]).strip()
        victim = " ".join(b.text for b in row[i + 1:]).strip()
    if not killer or not victim:
        return None
    conf = sum(b.conf for b in row) / len(row)
    return Row(t_s=t_s, killer=killer, victim=victim, distance_m=dist, raw=raw, conf=round(conf, 3), boxes=row)


def read_rows(boxes: list[Box], t_s: float, row_h: int, aliases: list[str], squad: list[str]) -> list[Row]:
    out = []
    for row in group_rows(boxes, row_h):
        r = parse_row(row, t_s)
        if r is None:
            continue
        if name_matches(r.killer, aliases):
            r.role = "kill"
        elif name_matches(r.victim, aliases):
            r.role = "death"
        elif squad and name_matches(r.killer, squad):
            r.role = "squad_kill"
        elif squad and name_matches(r.victim, squad):
            r.role = "squad_death"
        out.append(r)
    return out
