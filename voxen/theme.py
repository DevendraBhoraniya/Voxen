"""Centralized design tokens for Voxen.

All UI modules should import from here instead of hard-coding
colors, spacing, radii, or animation durations. Keeping tokens in
one place makes the visual identity consistent and easy to tune.
"""

from PySide6.QtGui import QColor, QFont


# ---------------------------------------------------------------------------
# Color palette (dark, restrained, premium)
# ---------------------------------------------------------------------------
class Color:
    BG = "#101114"
    SURFACE = "#191B20"
    ELEVATED = "#22252C"
    BORDER = "#30333B"
    BORDER_SUBTLE = "#262930"

    TEXT_PRIMARY = "#F3F4F6"
    TEXT_SECONDARY = "#9CA3AF"
    TEXT_MUTED = "#6B7280"
    TEXT_DISABLED = "#4B5563"

    ACCENT = "#8B7CFF"
    ACCENT_DIM = "#5B4FD6"
    ACCENT_HOVER = "#9D90FF"

    RECORD = "#FF6B76"
    RECORD_DIM = "#A83A42"

    SUCCESS = "#65D6A6"
    SUCCESS_DIM = "#3FA87A"

    WARNING = "#F5A623"
    ERROR = "#FF5A65"

    WAVEFORM = "#8B7CFF"
    WAVEFORM_DIM = "#3D3580"

    OVERLAY_BG = "#1A1B20"
    OVERLAY_BORDER = "#2E3038"


# ---------------------------------------------------------------------------
# Typography
# ---------------------------------------------------------------------------
class Font:
    FAMILY_PRIMARY = "Segoe UI"   # fallback if Inter is not installed
    FAMILY_MONO = "Consolas"

    SIZE_XS = 10
    SIZE_SM = 11
    SIZE_MD = 13
    SIZE_LG = 15
    SIZE_XL = 18
    SIZE_XXL = 22

    WEIGHT_REGULAR = 400
    WEIGHT_MEDIUM = 500
    WEIGHT_SEMIBOLD = 600
    WEIGHT_BOLD = 700


# ---------------------------------------------------------------------------
# Spacing (4 px grid)
# ---------------------------------------------------------------------------
class Space:
    XS = 4
    SM = 8
    MD = 12
    LG = 16
    XL = 24
    XXL = 32
    XXXL = 48


# ---------------------------------------------------------------------------
# Border radii
# ---------------------------------------------------------------------------
class Radius:
    SM = 6
    MD = 10
    LG = 14
    XL = 20
    PILL = 28
    FULL = 999


# ---------------------------------------------------------------------------
# Animation durations (ms)
# ---------------------------------------------------------------------------
class Anim:
    INSTANT = 80
    FAST = 150
    NORMAL = 200
    SLOW = 300
    OVERLAY_IN = 220
    OVERLAY_OUT = 180
    PULSE = 1200


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------
def qcolor(hex_str: str) -> QColor:
    return QColor(hex_str)


def font(family: str = Font.FAMILY_PRIMARY, size: int = Font.SIZE_MD,
         weight: int = Font.WEIGHT_REGULAR) -> QFont:
    f = QFont(family, size)
    f.setWeight(weight)
    return f
