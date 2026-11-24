"""
Colour science helpers.

Why CIELAB + ΔE2000: RGB/HEX distance does not match human perception — two
greys 10 RGB units apart can look identical while two blues 10 units apart look
different. ΔE2000 (CIE 2000) is the industry standard used by paint brands'
own colour-matching tools; ΔE < 1 is imperceptible, < 2.3 is a "just noticeable
difference", < 5 is a close match.
"""
from __future__ import annotations

import math
import re

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def normalize_hex(value: str) -> str:
    """'abc123' / '#ABC123' → '#ABC123'. Raises ValueError otherwise."""
    m = _HEX_RE.match(value.strip())
    if not m:
        raise ValueError(f"Invalid HEX colour: {value!r}")
    return "#" + m.group(1).upper()


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = normalize_hex(hex_color)[1:]
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _srgb_to_linear(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def hex_to_lab(hex_color: str) -> tuple[float, float, float]:
    """sRGB (D65) → CIE XYZ → CIELAB."""
    r, g, b = (_srgb_to_linear(c) for c in hex_to_rgb(hex_color))
    x = (r * 0.4124564 + g * 0.3575761 + b * 0.1804375) / 0.95047
    y = (r * 0.2126729 + g * 0.7151522 + b * 0.0721750) / 1.00000
    z = (r * 0.0193339 + g * 0.1191920 + b * 0.9503041) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e_2000(lab1: tuple[float, float, float], lab2: tuple[float, float, float]) -> float:
    """CIEDE2000 colour difference (Sharma, Wu & Dalal 2005 reference implementation)."""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    C1, C2 = math.hypot(a1, b1), math.hypot(a2, b2)
    C_bar = (C1 + C2) / 2
    G = 0.5 * (1 - math.sqrt(C_bar ** 7 / (C_bar ** 7 + 25 ** 7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360
    h2p = math.degrees(math.atan2(b2, a2p)) % 360

    dLp = L2 - L1
    dCp = C2p - C1p
    if C1p * C2p == 0:
        dhp = 0.0
    elif abs(h2p - h1p) <= 180:
        dhp = h2p - h1p
    elif h2p - h1p > 180:
        dhp = h2p - h1p - 360
    else:
        dhp = h2p - h1p + 360
    dHp = 2 * math.sqrt(C1p * C2p) * math.sin(math.radians(dhp / 2))

    Lp_bar = (L1 + L2) / 2
    Cp_bar = (C1p + C2p) / 2
    if C1p * C2p == 0:
        hp_bar = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hp_bar = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hp_bar = (h1p + h2p + 360) / 2
    else:
        hp_bar = (h1p + h2p - 360) / 2

    T = (1 - 0.17 * math.cos(math.radians(hp_bar - 30))
         + 0.24 * math.cos(math.radians(2 * hp_bar))
         + 0.32 * math.cos(math.radians(3 * hp_bar + 6))
         - 0.20 * math.cos(math.radians(4 * hp_bar - 63)))
    d_theta = 30 * math.exp(-(((hp_bar - 275) / 25) ** 2))
    Rc = 2 * math.sqrt(Cp_bar ** 7 / (Cp_bar ** 7 + 25 ** 7))
    Sl = 1 + (0.015 * (Lp_bar - 50) ** 2) / math.sqrt(20 + (Lp_bar - 50) ** 2)
    Sc = 1 + 0.045 * Cp_bar
    Sh = 1 + 0.015 * Cp_bar * T
    Rt = -math.sin(math.radians(2 * d_theta)) * Rc

    return math.sqrt(
        (dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2 + Rt * (dCp / Sc) * (dHp / Sh)
    )


def relative_luminance(hex_color: str) -> float:
    """WCAG relative luminance; ×100 approximates a paint's LRV."""
    r, g, b = (_srgb_to_linear(c) for c in hex_to_rgb(hex_color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b
