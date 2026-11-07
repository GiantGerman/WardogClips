"""HUD profiles: where on the frame the reward ticker, the kill feed and the money readout
live. Measured on Season 2 footage at 3440x1440. Other resolutions
scale from that base; the ticker is anchored to the right edge, the feed to the left.
"""

from dataclasses import dataclass

BASE_W, BASE_H = 3440, 1440

# name -> (x, y, w, h) at 3440x1440; "money_y" = the row below which ticker LINES live
# (above it: the profit | account readout)
PROFILES = {
    "s2-3440x1440": {
        "ticker": (2900, 0, 540, 160),
        "feed": (0, 560, 620, 300),
        "money_y": 70,
        "feed_row_h": 26,
    },
}
DEFAULT_PROFILE = "s2-3440x1440"


@dataclass(frozen=True)
class Roi:
    name: str
    x: int
    y: int
    w: int
    h: int

    @property
    def box(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.w, self.h


def profile_for(width: int | None, height: int | None, name_utc: float | None = None, setting: str = "auto") -> str:
    """Which profile a recording uses. Only one exists today; the date hook is here for when
    a patch moves the HUD (keyed on name_utc) so old footage keeps its own profile."""
    if setting and setting != "auto" and setting in PROFILES:
        return setting
    return DEFAULT_PROFILE


def rois(width: int, height: int, profile: str = DEFAULT_PROFILE) -> dict[str, Roi]:
    """ROIs for a frame of this size: the HUD scales with the height and hugs the edges."""
    p = PROFILES.get(profile, PROFILES[DEFAULT_PROFILE])
    s = height / BASE_H
    # every size / offset EVEN: the frames are 4:2:0, and the keyframe pass pads + vstacks the crops --
    # an odd width (1080p: the ticker scales to 405 px; a 1080x1920 TikTok: 827) fails the whole pass
    ev = lambda v: max(2, int(v) // 2 * 2)   # noqa: E731
    out = {}
    for name in ("ticker", "feed"):
        x, y, w, h = p[name]
        w2, h2 = min(ev(round(w * s)), ev(width)), min(ev(round(h * s)), ev(height))
        y2 = int(round(y * s))
        if name == "ticker":
            # right-anchored by its (scaled, even) right margin, so it stays flush with the edge
            x2 = width - w2 - ev(round((BASE_W - x - w) * s)) if BASE_W - x - w > 0 else width - w2
            x2 = max(0, x2 - (x2 % 2))
        else:
            x2 = ev(max(0, min(width - w2, int(round(x * s))))) if x > 0 else 0
        y2 = ev(max(0, min(height - h2, y2))) if y2 > 0 else 0
        out[name] = Roi(name, x2, y2, w2, h2)
    return out


# the centred `RESPAWNING IN` text at 3440x1440 (OCR'd to confirm a death without a feed row)
CENTER_BASE = (1400, 440)


def respawn_roi(width: int, height: int) -> Roi:
    """The built-in respawn-text box: 1400x440 px centred, NOT scaled (as detect has always read it;
    core/zones.py scales it with the height for templates and the zones editor's default)."""
    cw, ch = CENTER_BASE
    return Roi("center", max(0, width // 2 - cw // 2), max(0, height // 2 - ch // 2), min(width, cw), min(height, ch))


def refine_center_roi(width: int, height: int) -> Roi:
    """The built-in 400x200 centre box whose darkness times a death to the frame (detect pass 3)."""
    return Roi("center", width // 2 - 200, height // 2 - 100, 400, 200)


def scale(height: int, profile: str = DEFAULT_PROFILE) -> float:
    return height / BASE_H


def money_y(height: int, profile: str = DEFAULT_PROFILE) -> int:
    p = PROFILES.get(profile, PROFILES[DEFAULT_PROFILE])
    return int(round(p["money_y"] * height / BASE_H))


def feed_row_h(height: int, profile: str = DEFAULT_PROFILE) -> int:
    p = PROFILES.get(profile, PROFILES[DEFAULT_PROFILE])
    return max(10, int(round(p["feed_row_h"] * height / BASE_H)))
