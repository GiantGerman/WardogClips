r"""The reward ticker: the lines WARDOGS prints under the money readout, top-right.

Seen on Season 2 footage: `KILL +$1,500`, `KILL 250XP`,
`SPOTTED TARGET DESTROYED +$400`, `TARGET SPOTTED 25XP`, `TARGET SPOTTED REFRESHED`,
`CONTROL ZONE PRESENCE +$160`, `CONTROL ZONE ENTERED +$250`, `SUPPLIES DEPOSITED +$720`,
`REVIVED TEAMMATE 200XP`, `REVIVED PLAYER ASSIST +$200`, `TIP - <name> +$10`.
Kill assists: `KILL ASSIST +$1,381` (OCR often reads `KILLASSIST`); a bare `ASSIST` still counts. NOT
`VEHICLE DESTROYED ASSIST +$...` / `75XP`: its own label, no event -- read with its spaces (the fast ticker engine
keeps them) a generic `\bASSIST\b` would make it a kill assist. The calibration page lets the user map any raw text
to a label.
"""

import re
from dataclasses import dataclass, field

from vision.ocr import Box

# label -> compiled rule; order matters (first match wins)
_BUILTIN = [
    ("revive_assist", re.compile(r"REVIVED?\s*PLAYER\s*ASSIST")),
    ("waited_revive", re.compile(r"WAITED\s*TO\s*BE\s*REVIVED")),
    ("revive", re.compile(r"(HELPED\s*)?REVIVED?\s*(TEAMMATE|PLAYER)|HELPED\s*REVIVE")),
    ("heal", re.compile(r"HEALED\s*TEAMMATE")),
    ("vehicle_assist", re.compile(r"VEHICLE\s*DESTROYED\s*ASSIST")),
    # 'KILLASSST' / 'KILLASIST' are RapidOCR misreads of KILL ASSIST
    ("assist", re.compile(r"\b(KILL\s*AS{1,3}I?S?T\b|ASSIST(ED)?\b)")),
    ("kill", re.compile(r"^\W*KILL\b")),
    ("headshot", re.compile(r"\bHEADSHOT\b")),
    ("spotted_destroyed", re.compile(r"SPOTTED\s*(TARGET|VEHICLE)\s*DESTROYED")),
    ("spotted", re.compile(r"TARGET\s*SPOTTED")),
    ("cz_presence", re.compile(r"CONTROL\s*ZONE\s*PRESENCE")),
    ("cz_entered", re.compile(r"CONTROL\s*ZONE\s*ENTERED")),
    ("supplies", re.compile(r"SUPPLIES\s*DEPOSITED")),
    ("tip", re.compile(r"^\W*TIP\b")),
]
EVENT_LABELS = {"kill": "kill", "assist": "assist", "headshot": "kill", "spotted_destroyed": "spotted",
                "revive": "revive", "revive_assist": "revive"}
KNOWN_LABELS = ["kill", "assist", "headshot", "spotted_destroyed", "spotted", "revive", "revive_assist", "heal",
                "waited_revive", "vehicle_assist", "cz_presence", "cz_entered", "supplies", "tip", "other"]

_CASH = re.compile(r"[+\-]?\s*[\$S]\s?(\d{1,3}(?:[,.]\d{3})+|\d+)")
_XP = re.compile(r"(\d+)\s*X[PF]\b")                     # "75XF" is OCR for 75XP
_MONEY = re.compile(r"(-?)\s*[\$S]\s?(\d{1,3}(?:[,.]\d{3})+|\d+)")   # OCR reads "$" as "S" and "," as "." now and then


@dataclass
class Line:
    t_s: float
    text: str
    label: str
    cash: int | None = None
    xp: int | None = None
    conf: float = 0.0
    boxes: list = field(default_factory=list)

    @property
    def y0(self) -> int:
        return min((b.y0 for b in self.boxes), default=0)

    @property
    def y1(self) -> int:
        return max((b.y1 for b in self.boxes), default=0)

    @property
    def x0(self) -> int:
        return min((b.x0 for b in self.boxes), default=0)

    @property
    def x1(self) -> int:
        return max((b.x1 for b in self.boxes), default=0)


def normalise(text: str) -> str:
    """OCR noise -> something the rules can read: upper-case, O->0 inside digit runs,
    stray punctuation collapsed."""
    t = text.upper().strip()
    t = re.sub(r"[|]", "I", t)
    t = re.sub(r"(?<=\d)O|O(?=\d)", "0", t)        # 25OXP -> 250XP
    t = re.sub(r"(?<=\d)I|I(?=\d)", "1", t)        # SI60 -> S160
    t = re.sub(r"Z(?=\d)", "2", t)                 # Z5XP -> 25XP
    t = re.sub(r"\s+", " ", t)
    return t


def _amount(s: str) -> int | None:
    try:
        return int(s.replace(",", "").replace(".", ""))
    except ValueError:
        return None


def parse_line(text: str, user_rules: list[tuple[str, str]] | None = None) -> tuple[str, int | None, int | None]:
    """(label, cash, xp). User rules are (substring-or-regex, label) and win over the
    built-ins; unknown text is 'other'."""
    t = normalise(text)
    cash = xp = None
    m = _XP.search(t)
    if m:
        xp = _amount(m.group(1))
    body = _XP.sub(" ", t)
    m = _CASH.search(body)
    if m:
        cash = _amount(m.group(1))
    label = None
    for pat, lab in (user_rules or []):
        try:
            if re.search(pat, t, re.I):
                label = lab
                break
        except re.error:
            if pat.upper() in t:
                label = lab
                break
    if label is None:
        for lab, rx in _BUILTIN:
            if rx.search(t):
                label = lab
                break
    return label or "other", cash, xp


def group_rows(boxes: list[Box], row_h: int) -> list[list[Box]]:
    """Boxes -> rows by vertical centre, each row left-to-right."""
    rows: list[list[Box]] = []
    for b in sorted(boxes, key=lambda b: (b.cy, b.cx)):
        for row in rows:
            if abs(row[0].cy - b.cy) <= row_h * 0.6:
                row.append(b)
                break
        else:
            rows.append([b])
    for row in rows:
        row.sort(key=lambda b: b.cx)
    rows.sort(key=lambda r: r[0].cy)
    return rows


def read_money(boxes: list[Box], money_y: int) -> tuple[int | None, int | None]:
    """(profit, account) from the readout above money_y: the two $ figures left to right."""
    vals = []
    for b in sorted((b for b in boxes if b.cy < money_y), key=lambda b: b.cx):
        for m in _MONEY.finditer(normalise(b.text)):
            v = _amount(m.group(2))
            if v is not None:
                vals.append(-v if m.group(1) == "-" else v)
    if not vals:
        return None, None
    if len(vals) == 1:
        return None, vals[0]
    return vals[0], vals[-1]


def read_lines(boxes: list[Box], t_s: float, money_y: int, row_h: int,
               user_rules: list[tuple[str, str]] | None = None) -> list[Line]:
    """Ticker lines below the money readout, one Line per visual row."""
    below = [b for b in boxes if b.cy >= money_y]
    out = []
    for row in group_rows(below, row_h):
        text = " ".join(b.text for b in row).strip()
        if len(text) < 3:
            continue
        label, cash, xp = parse_line(text, user_rules)
        conf = sum(b.conf for b in row) / len(row)
        out.append(Line(t_s=t_s, text=text, label=label, cash=cash, xp=xp, conf=round(conf, 3), boxes=row))
    return out
