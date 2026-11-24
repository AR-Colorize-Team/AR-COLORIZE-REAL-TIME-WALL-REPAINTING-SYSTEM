"""
Reference implementation of the live wall-paint renderer.

This NumPy code is the *golden model* for the GPU shaders
(ios/.../WallPaint.metal and android/.../wall_paint.frag). Each shader function
has a 1:1 counterpart here, so behaviour can be unit-tested on a laptop and the
shaders compared against it pixel-for-pixel.

Pipeline per camera frame (all on the GPU, ~1–2 ms at 1080p on a mid-range phone):

    camera RGB ─┬─► linearise ─► luminance Y ─┐
                │                              ├─► shading s = Y / Ŷ_wall  (keeps shadows, light
                │   wall mask (soft, 0..1) ────┘        falloff and plaster texture)
                │
    paint colour T (+ pattern colour per wall-UV) ─► T_lit = T · light_tint
                                                       out = softclip(T_lit · s)  (+ gloss sheen)
                                                       final = mix(camera, out, mask · opacity)

Why this looks real: paint changes the wall's *albedo*, not the light falling on
it. Multiplying the new albedo by the observed shading ratio keeps every shadow,
lamp gradient and wall texture the camera sees, so a "matte sage" wall still
darkens behind the sofa exactly as the real wall does.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# ----------------------------------------------------------------------------
# Colour space helpers (match the shader's srgbToLinear / linearToSrgb exactly)
# ----------------------------------------------------------------------------
LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)   # Rec.709 / sRGB primaries


def srgb_to_linear(c: np.ndarray) -> np.ndarray:
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)


def linear_to_srgb(c: np.ndarray) -> np.ndarray:
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055).astype(np.float32)


def hex_to_linear(hex_color: str) -> np.ndarray:
    h = hex_color.lstrip("#")
    rgb = np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.float32) / 255.0
    return srgb_to_linear(rgb)


# ----------------------------------------------------------------------------
# Finishes: how much of the original specular highlight survives, and sheen.
# Mirrors `FinishParams` in the shaders.
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class Finish:
    specular_keep: float   # fraction of the original highlight energy re-applied
    sheen: float           # extra soft reflection in already-bright areas


FINISHES = {
    "flat":       Finish(0.00, 0.00),
    "matte":      Finish(0.05, 0.00),
    "eggshell":   Finish(0.15, 0.03),
    "satin":      Finish(0.30, 0.06),
    "semi_gloss": Finish(0.50, 0.10),
    "gloss":      Finish(0.70, 0.15),
}


def soft_clip(x: np.ndarray, knee: float = 0.8) -> np.ndarray:
    """Smooth roll-off above `knee` so pale paints in bright light don't clip to flat white."""
    over = np.maximum(x - knee, 0.0)
    return np.minimum(x, knee) + (1.0 - knee) * (1.0 - np.exp(-over / (1.0 - knee)))


# ----------------------------------------------------------------------------
# Core recolour
# ----------------------------------------------------------------------------
def wall_reference_luminance(frame_lin: np.ndarray, mask: np.ndarray) -> float:
    """
    Ŷ_wall: robust 'typical wall brightness'. Median of masked luminance (GPU: a
    mask-weighted mean read from a 1×1 mip, smoothed over ~10 frames so camera
    auto-exposure changes don't flicker the paint).
    """
    y = frame_lin @ LUMA
    sel = y[mask > 0.5]
    return float(np.median(sel)) if sel.size else float(np.median(y))


def estimate_light_tint(frame_srgb: np.ndarray, mask: np.ndarray,
                        old_wall_hex: str | None = None, strength: float = 0.8) -> np.ndarray:
    """
    Colour of the light falling on the wall, so new paint picks up the room's warm
    bulbs or cool daylight. Divides the wall's observed average colour by its known
    (or assumed neutral) albedo colour. On device, ARKit/ARCore light estimation
    provides this directly; this estimator is the fallback and the reference.
    """
    lin = srgb_to_linear(frame_srgb)
    sel = lin[mask > 0.5]
    if sel.size == 0:
        return np.ones(3, np.float32)
    observed = sel.mean(axis=0)
    albedo = hex_to_linear(old_wall_hex) if old_wall_hex else np.ones(3, np.float32)
    ratio = observed / np.maximum(albedo, 1e-4)
    tint = ratio / max(float(ratio @ LUMA), 1e-4)          # normalise: luminance 1
    return (1 - strength + strength * tint).astype(np.float32)


def recolor(frame_srgb: np.ndarray, mask: np.ndarray, paint_lin: np.ndarray,
            *, finish: str = "matte", opacity: float = 1.0,
            light_tint: np.ndarray | None = None, y_ref: float | None = None,
            old_wall_albedo: float = 0.65, contrast_k: float = 0.85) -> np.ndarray:
    """
    frame_srgb      : H×W×3 float32 in 0..1 (camera image)
    mask            : H×W float32 0..1 (soft wall mask; doors/windows/switches already excluded)
    paint_lin       : 3 or H×W×3 linear-RGB paint albedo (per-pixel when a pattern is applied)
    light_tint      : linear RGB scene-light colour from AR light estimation (white = [1,1,1])
    old_wall_albedo : estimated luminance reflectance of the *current* wall (0.65 ≈ typical
                      off-white; the AI analysis refines it per room)
    contrast_k      : 1 = physically exact relative brightness; slightly lower keeps very dark
                      paints readable on phone screens.

    Exposure model: the camera exposed the old wall (albedo ρ_old) to brightness Ŷ_wall.
    Under the same light and exposure a paint of luminance ρ_new appears at
    Ŷ_wall · (ρ_new / ρ_old). So navy really reads dark and white really reads bright.
    """
    fp = FINISHES.get(finish, FINISHES["matte"])
    lin = srgb_to_linear(frame_srgb)
    y = lin @ LUMA
    y_ref = max(wall_reference_luminance(lin, mask) if y_ref is None else y_ref, 1e-4)

    # Shading ratio: 1.0 on a typical wall pixel, <1 in shadow, >1 near lamps/windows.
    s = (y / y_ref)[..., None]

    tint = np.ones(3, np.float32) if light_tint is None else np.asarray(light_tint, np.float32)
    paint = np.broadcast_to(np.asarray(paint_lin, np.float32), lin.shape)
    paint_y = (paint @ LUMA)[..., None]
    direction = np.where(paint_y > 1e-5, paint / np.maximum(paint_y, 1e-5), 0.0)   # chroma only
    level = y_ref * np.power(np.maximum(paint_y, 0) / old_wall_albedo, contrast_k)  # brightness
    out = direction * level * tint * s

    # Finish: energy above the diffuse ceiling is specular reflection; glossier paints keep more.
    spec = np.maximum(s - 1.3, 0.0) * y_ref
    out = out + (fp.specular_keep + fp.sheen) * spec

    out_srgb = linear_to_srgb(soft_clip(out))
    a = np.clip(mask, 0, 1) * opacity
    a = np.where(a < 1 / 512, 0.0, a)[..., None]   # GPU: early-out, so off-wall pixels stay bit-exact
    return np.where(a > 0, frame_srgb * (1 - a) + out_srgb * a, frame_srgb).astype(np.float32)


# ----------------------------------------------------------------------------
# World-locked wall patterns
# (u, v) are metres on the wall plane: u along the wall, v up from the floor.
# Because they come from the AR plane anchor, stripes stay glued to the wall as
# the user walks around — they don't slide with the screen.
# ----------------------------------------------------------------------------
def _aa_step(edge: float | np.ndarray, x: np.ndarray, px: float) -> np.ndarray:
    """Anti-aliased step (GPU: smoothstep(edge - fwidth, edge + fwidth, x))."""
    return np.clip((x - edge) / max(px, 1e-6) * 0.5 + 0.5, 0.0, 1.0)


def pattern_weights(pattern: str, u: np.ndarray, v: np.ndarray, params: dict, px: float = 0.004) -> np.ndarray:
    """
    Returns w in 0..1: 0 → colour A, 1 → colour B. `px` ≈ metres per screen pixel
    (GPU: fwidth(uv)) for crisp, alias-free edges at any distance.
    """
    p = params
    if pattern == "solid":
        return np.zeros_like(u)
    if pattern == "two_tone":            # lower band (wainscot) in colour B
        return 1.0 - _aa_step(p.get("split_m", 0.95), v, px)
    if pattern == "stripes_vertical":
        w = p.get("stripe_m", 0.30)
        t = np.mod(u, 2 * w)
        return _aa_step(w, t, px) * (1 - _aa_step(2 * w - px, t, px))
    if pattern == "stripes_horizontal":
        w = p.get("stripe_m", 0.25)
        t = np.mod(v, 2 * w)
        return _aa_step(w, t, px) * (1 - _aa_step(2 * w - px, t, px))
    if pattern == "ombre":               # soft vertical gradient A (top) → B (bottom)
        lo, hi = p.get("from_m", 0.3), p.get("to_m", 2.2)
        t = np.clip((v - lo) / max(hi - lo, 1e-3), 0, 1)
        return 1.0 - t * t * (3 - 2 * t)
    if pattern == "mountain":            # geometric peaks along the lower wall
        period, base, amp = p.get("period_m", 1.6), p.get("base_m", 0.9), p.get("amp_m", 0.7)
        tri = 1 - np.abs(2 * np.mod(u / period, 1.0) - 1)            # 0..1..0
        return 1.0 - _aa_step(base + amp * tri, v, px)
    if pattern == "arch":                # painted arch, e.g. behind a bed or desk
        cx, width, height = p.get("center_m", 1.5), p.get("width_m", 1.4), p.get("height_m", 2.1)
        r = width / 2
        spring = height - r
        inside_rect = (1 - _aa_step(r, np.abs(u - cx), px)) * (1 - _aa_step(spring, v, px))
        d = np.sqrt((u - cx) ** 2 + (v - spring) ** 2)
        inside_dome = (1 - _aa_step(r, d, px)) * _aa_step(spring, v, px)
        return np.clip(inside_rect + inside_dome, 0, 1)
    if pattern == "color_block":         # offset rectangle accent
        x0, x1 = p.get("x0_m", 0.4), p.get("x1_m", 1.6)
        y0, y1 = p.get("y0_m", 0.0), p.get("y1_m", 1.8)
        return (_aa_step(x0, u, px) * (1 - _aa_step(x1, u, px))
                * _aa_step(y0, v, px) * (1 - _aa_step(y1, v, px)))
    raise ValueError(f"unknown pattern {pattern}")


PATTERNS = ("solid", "two_tone", "stripes_vertical", "stripes_horizontal",
            "ombre", "mountain", "arch", "color_block")


def pattern_paint(pattern: str, u: np.ndarray, v: np.ndarray, color_a: str, color_b: str,
                  params: dict | None = None, px: float = 0.004) -> np.ndarray:
    """Per-pixel linear paint albedo for a pattern (blend in linear space)."""
    w = pattern_weights(pattern, u, v, params or {}, px)[..., None]
    return hex_to_linear(color_a) * (1 - w) + hex_to_linear(color_b) * w
