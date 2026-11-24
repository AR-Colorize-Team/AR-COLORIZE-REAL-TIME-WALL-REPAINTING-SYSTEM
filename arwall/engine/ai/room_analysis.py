"""
Premium AI room analysis.

Two layers, so the result is both *grounded* and *insightful*:

1. Measurements (deterministic CV, ~40 ms): light colour temperature (K), brightness,
   light direction, current wall colour + LRV + undertone, dominant furnishing
   colours. These are facts, not guesses.
2. Designer reasoning (Claude vision, structured output): style, constraints and
   three psychologically-matched palettes — *given the measurements as evidence*,
   so the model doesn't hallucinate "bright south light" in a dim north room.

Then a validation pass enforces quality: valid HEX, distinct palettes (ΔE2000),
readable trim contrast, real paint matches, and lighting warnings. If the LLM
call fails, a curated rule-based recommender returns a full report anyway, so
the feature never shows an error to a paying user.

Output contract is consumed by the backend worker callback (Part 2,
routes/ai.py::_persist_ai_palettes) and the Flutter AI report screen.
"""
from __future__ import annotations

import base64
import json
import logging
import math
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import cv2
import numpy as np

log = logging.getLogger("arwall.ai")

MODEL = "claude-sonnet-5-5"          # best speed/intelligence balance with vision; see platform.claude.com/docs/en/models/overview
MAX_IMAGE_SIDE = 1568                # long edge sent to the model (enough detail, bounded tokens)

# ---------------------------------------------------------------------------
# Colour utilities (CIELAB, CIEDE2000 shared with the backend)
# ---------------------------------------------------------------------------
_M_RGB2XYZ = np.array([[0.4124564, 0.3575761, 0.1804375],
                       [0.2126729, 0.7151522, 0.0721750],
                       [0.0193339, 0.1191920, 0.9503041]], np.float64)


def srgb_to_linear(c: np.ndarray) -> np.ndarray:
    c = np.clip(c, 0, 1)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(c: np.ndarray) -> np.ndarray:
    c = np.clip(c, 0, 1)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055)


def hex_to_rgb01(h: str) -> np.ndarray:
    h = h.strip().lstrip("#")
    if len(h) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in h):
        raise ValueError(f"bad hex {h!r}")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], np.float64) / 255


def rgb01_to_hex(rgb: np.ndarray) -> str:
    r, g, b = (int(round(float(np.clip(x, 0, 1)) * 255)) for x in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def normalize_hex(h: str) -> str:
    return rgb01_to_hex(hex_to_rgb01(h))


def rgb_to_lab(rgb01: np.ndarray) -> np.ndarray:
    xyz = srgb_to_linear(np.asarray(rgb01, np.float64)) @ _M_RGB2XYZ.T
    xyz = xyz / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def lrv(hex_color: str) -> float:
    """Light Reflectance Value ≈ relative luminance × 100 (what paint brands publish)."""
    return float(srgb_to_linear(hex_to_rgb01(hex_color)) @ _M_RGB2XYZ[1] * 100)


def delta_e(h1: str, h2: str) -> float:
    """CIEDE2000 between two HEX colours."""
    L1, a1, b1 = rgb_to_lab(hex_to_rgb01(h1))
    L2, a2, b2 = rgb_to_lab(hex_to_rgb01(h2))
    C1, C2 = math.hypot(a1, b1), math.hypot(a2, b2)
    Cb = (C1 + C2) / 2
    G = 0.5 * (1 - math.sqrt(Cb ** 7 / (Cb ** 7 + 25 ** 7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p, h2p = math.degrees(math.atan2(b1, a1p)) % 360, math.degrees(math.atan2(b2, a2p)) % 360
    dL, dC = L2 - L1, C2p - C1p
    dh = 0.0 if C1p * C2p == 0 else (h2p - h1p if abs(h2p - h1p) <= 180 else
                                     h2p - h1p - 360 if h2p - h1p > 180 else h2p - h1p + 360)
    dH = 2 * math.sqrt(C1p * C2p) * math.sin(math.radians(dh / 2))
    Lb, Cpb = (L1 + L2) / 2, (C1p + C2p) / 2
    hb = (h1p + h2p if C1p * C2p == 0 else (h1p + h2p) / 2 if abs(h1p - h2p) <= 180 else
          (h1p + h2p + 360) / 2 if h1p + h2p < 360 else (h1p + h2p - 360) / 2)
    T = (1 - 0.17 * math.cos(math.radians(hb - 30)) + 0.24 * math.cos(math.radians(2 * hb))
         + 0.32 * math.cos(math.radians(3 * hb + 6)) - 0.20 * math.cos(math.radians(4 * hb - 63)))
    Rc = 2 * math.sqrt(Cpb ** 7 / (Cpb ** 7 + 25 ** 7))
    Sl = 1 + 0.015 * (Lb - 50) ** 2 / math.sqrt(20 + (Lb - 50) ** 2)
    Sc, Sh = 1 + 0.045 * Cpb, 1 + 0.015 * Cpb * T
    Rt = -math.sin(math.radians(2 * 30 * math.exp(-(((hb - 275) / 25) ** 2)))) * Rc
    return math.sqrt((dL / Sl) ** 2 + (dC / Sc) ** 2 + (dH / Sh) ** 2 + Rt * (dC / Sc) * (dH / Sh))


def undertone(hex_color: str) -> str:
    """warm | cool | green | neutral — what decorators mean by a colour's undertone."""
    _, a, b = rgb_to_lab(hex_to_rgb01(hex_color))
    if math.hypot(a, b) < 3.0:
        return "neutral"
    hue = math.degrees(math.atan2(b, a)) % 360
    if 20 <= hue < 100:
        return "warm"
    if 100 <= hue < 170:
        return "green"
    if 170 <= hue < 300:
        return "cool"
    return "warm"          # pinks/reds read warm on walls


# ---------------------------------------------------------------------------
# 1. Measurements
# ---------------------------------------------------------------------------
@dataclass
class Measurements:
    color_temperature_k: int
    light_label: str              # "warm incandescent" … "cool daylight"
    brightness: str               # dim | medium | bright
    mean_luminance: float
    light_direction: str          # left | right | above | even
    current_wall_hex: str | None
    current_wall_lrv: float | None
    current_wall_undertone: str | None
    dominant_colors: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _cct_mccamy(rgb_lin: np.ndarray) -> float:
    X, Y, Z = rgb_lin @ _M_RGB2XYZ.T
    s = X + Y + Z
    if s <= 1e-9:
        return 6500.0
    x, y = X / s, Y / s
    n = (x - 0.3320) / (0.1858 - y)
    return float(449 * n ** 3 + 3525 * n ** 2 + 6823.3 * n + 5520.33)


def estimate_illuminant(lin: np.ndarray, wall_mask: np.ndarray | None) -> np.ndarray:
    """
    Shades-of-grey (Minkowski p=6) over near-neutral, well-exposed pixels — more robust
    than gray-world when a room has a big coloured sofa. Walls are the best probe.
    """
    px = lin.reshape(-1, 3)
    if wall_mask is not None and (wall_mask > 0.5).sum() > 500:
        px = lin[wall_mask > 0.5]
    y = px @ _M_RGB2XYZ[1]
    chroma = (px.max(1) - px.min(1)) / np.maximum(px.max(1), 1e-6)
    sel = px[(y > np.percentile(y, 20)) & (y < np.percentile(y, 99)) & (chroma < 0.45)]
    if len(sel) < 200:
        sel = px
    p = 6
    return np.power(np.mean(np.power(sel, p), axis=0), 1 / p)


def _kelvin_label(k: float) -> str:
    return ("warm incandescent" if k < 3200 else "warm white" if k < 4200 else
            "neutral daylight" if k < 5600 else "cool daylight")


_NAMES = {  # small vocabulary for readable dominant colours
    "#F5F5F0": "white", "#E8E0D2": "cream", "#C8B8A0": "beige", "#8B6B4A": "walnut", "#C49A6C": "oak",
    "#5A5A5A": "charcoal", "#A0A0A0": "grey", "#1E1E1E": "black", "#2F4B3A": "forest green",
    "#8FA382": "sage", "#2E3F5C": "navy", "#7FA1B3": "dusty blue", "#B5533C": "rust",
    "#D9A441": "mustard", "#C98C8C": "dusty pink", "#6B4E71": "plum",
}


def color_name(hex_color: str) -> str:
    return _NAMES[min(_NAMES, key=lambda n: delta_e(hex_color, n))]


def measure_room(image_bgr: np.ndarray, wall_mask: np.ndarray | None = None) -> Measurements:
    img = cv2.resize(image_bgr, (512, int(512 * image_bgr.shape[0] / image_bgr.shape[1])), interpolation=cv2.INTER_AREA)
    if wall_mask is not None:
        wall_mask = cv2.resize(wall_mask.astype(np.float32), (img.shape[1], img.shape[0]), interpolation=cv2.INTER_LINEAR)
    rgb = img[..., ::-1].astype(np.float64) / 255
    lin = srgb_to_linear(rgb)
    lum = lin @ _M_RGB2XYZ[1]

    illum = estimate_illuminant(lin, wall_mask)
    k = int(round(np.clip(_cct_mccamy(illum), 1800, 10000) / 50) * 50)

    mean_l = float(lum.mean())
    brightness = "dim" if mean_l < 0.08 else "bright" if mean_l > 0.30 else "medium"

    # Light direction from wall (or whole frame) luminance gradient.
    probe = lum if wall_mask is None else np.where(wall_mask > 0.5, lum, np.nan)
    w = probe.shape[1]
    left, right = np.nanmean(probe[:, : w // 3]), np.nanmean(probe[:, -w // 3:])
    top, bottom = np.nanmean(probe[: probe.shape[0] // 3]), np.nanmean(probe[-probe.shape[0] // 3:])
    ratio_lr = (left + 1e-6) / (right + 1e-6)
    direction = ("left" if ratio_lr > 1.25 else "right" if ratio_lr < 0.8 else
                 "above" if (top + 1e-6) / (bottom + 1e-6) > 1.25 else "even")

    wall_hex = wall_lrv = wall_tone = None
    if wall_mask is not None and (wall_mask > 0.5).sum() > 500:
        # Remove the illuminant cast so we report the paint colour, not the light colour.
        # 1) remove the light's colour cast; 2) undo camera exposure by anchoring the brightest
        #    neutral surfaces in the room (trim, ceiling, switch plates) to a typical white LRV ≈ 85.
        albedo = np.median(lin[wall_mask > 0.5], axis=0) / np.maximum(illum / (illum @ _M_RGB2XYZ[1]), 1e-6)
        flat = lin.reshape(-1, 3) / np.maximum(illum / (illum @ _M_RGB2XYZ[1]), 1e-6)   # cast-corrected
        neutral = flat[(flat.max(1) - flat.min(1)) / np.maximum(flat.max(1), 1e-6) < 0.15]
        if len(neutral) > 100:
            white_y = float(np.percentile(neutral @ _M_RGB2XYZ[1], 99))
            albedo = albedo * (0.85 / max(white_y, 1e-3))
        albedo = np.clip(albedo, 0, 0.95)
        wall_hex = rgb01_to_hex(linear_to_srgb(albedo))
        wall_lrv, wall_tone = round(lrv(wall_hex), 1), undertone(wall_hex)

    # Dominant furnishing colours: k-means in Lab over non-wall pixels.
    sel = rgb if wall_mask is None else rgb[wall_mask < 0.5]
    sel = sel.reshape(-1, 3)
    if len(sel) > 20000:
        sel = sel[np.random.default_rng(0).choice(len(sel), 20000, replace=False)]
    lab = rgb_to_lab(sel).astype(np.float32)
    K = 6
    _, labels, centers = cv2.kmeans(lab, K, None, (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.5),
                                    3, cv2.KMEANS_PP_CENTERS)
    shares = np.bincount(labels.ravel(), minlength=K) / len(labels)
    dominant = []
    for idx in np.argsort(-shares):
        members = sel[labels.ravel() == idx]
        hx = rgb01_to_hex(np.median(members, axis=0))
        if any(delta_e(hx, d["hex"]) < 6 or color_name(hx) == d["name"] for d in dominant):   # merge near-duplicates
            continue
        dominant.append({"hex": hx, "name": color_name(hx), "share": round(float(shares[idx]), 3),
                         "undertone": undertone(hx)})
    return Measurements(k, _kelvin_label(k), brightness, round(mean_l, 4), direction,
                        wall_hex, wall_lrv, wall_tone, dominant[:5])


# ---------------------------------------------------------------------------
# 2. Designer reasoning (Claude vision, forced tool call = guaranteed JSON shape)
# ---------------------------------------------------------------------------
REPORT_TOOL = {
    "name": "submit_room_report",
    "description": "Submit the interior colour analysis and exactly the requested number of palettes.",
    "input_schema": {
        "type": "object",
        "required": ["summary", "style", "room", "constraints", "palettes", "tips"],
        "properties": {
            "summary": {"type": "string", "description": "2 sentences, warm and specific to THIS room."},
            "style": {"type": "object", "required": ["detected", "confidence"],
                      "properties": {"detected": {"type": "string"}, "confidence": {"type": "number"}}},
            "room": {"type": "object", "required": ["type", "size", "est_ceiling_height_m"],
                     "properties": {"type": {"type": "string"}, "size": {"type": "string", "enum": ["small", "medium", "large"]},
                                    "est_ceiling_height_m": {"type": "number"},
                                    "materials": {"type": "array", "items": {"type": "string"}}}},
            "constraints": {"type": "array", "items": {"type": "string"},
                            "description": "Facts that limit colour choice, e.g. 'warm 2900 K bulbs make cool greys look muddy'."},
            "palettes": {"type": "array", "items": {
                "type": "object",
                "required": ["name", "tagline", "mood_tags", "psychology", "why_it_works", "colors", "suggested_design"],
                "properties": {
                    "name": {"type": "string"}, "tagline": {"type": "string"},
                    "mood_tags": {"type": "array", "items": {"type": "string"}},
                    "psychology": {"type": "string", "description": "How it will make occupants feel and why (colour psychology)."},
                    "why_it_works": {"type": "string", "description": "Reference the measured light, current furniture colours and style."},
                    "colors": {"type": "array", "minItems": 3, "maxItems": 5, "items": {
                        "type": "object", "required": ["role", "hex", "label"],
                        "properties": {"role": {"type": "string", "enum": ["primary", "secondary", "accent", "trim", "ceiling"]},
                                       "hex": {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"},
                                       "label": {"type": "string"},
                                       "finish": {"type": "string", "enum": ["matte", "eggshell", "satin", "semi_gloss"]}}}},
                    "suggested_design": {"type": "object", "required": ["pattern"],
                                         "properties": {"pattern": {"type": "string", "enum": [
                                             "solid", "two_tone", "stripes_vertical", "stripes_horizontal",
                                             "ombre", "mountain", "arch", "color_block"]},
                                             "reason": {"type": "string"}}},
                }}},
            "tips": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        },
    },
}

SYSTEM_PROMPT = """You are a senior interior colour consultant (think Farrow & Ball colour \
consultant meets environmental psychologist). You analyse a photo of a real room and \
recommend wall paint palettes that will look right IN THIS ROOM'S LIGHT.

Rules:
- The MEASUREMENTS block is ground truth from calibrated computer vision. Trust it over \
your impression of the photo (phone cameras auto-white-balance).
- Every palette must harmonise with the furniture/floor colours that will stay.
- Respect the light: under warm light (<3500 K) avoid cool blue-greys that turn muddy; in \
dim or north-facing rooms prefer primaries with LRV ≥ 50; very bright rooms can carry deep colours.
- The three palettes must be genuinely different directions (e.g. calm/airy, warm/sociable, \
bold/dramatic), not three shades of beige.
- Include a 'trim' colour with clear contrast to the primary and a 'ceiling' colour.
- Be specific and concrete. No filler. Speak to a homeowner, not a designer."""


class LLMClient(Protocol):
    def messages_create(self, **kwargs: Any) -> Any: ...


class AnthropicClient:
    """Thin adapter so tests can inject a fake. Production: pip install anthropic."""

    def __init__(self, api_key: str | None = None, timeout: float = 45.0):
        import anthropic  # imported lazily: only the GPU/AI worker needs it
        self._c = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)

    def messages_create(self, **kwargs: Any) -> Any:
        return self._c.messages.create(**kwargs)


def _encode_image(image_bgr: np.ndarray) -> str:
    h, w = image_bgr.shape[:2]
    scale = min(1.0, MAX_IMAGE_SIDE / max(h, w))
    if scale < 1:
        image_bgr = cv2.resize(image_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", image_bgr, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise RuntimeError("jpeg encode failed")
    return base64.b64encode(buf.tobytes()).decode()


def ask_designer(client: LLMClient, image_bgr: np.ndarray, m: Measurements, *, room_type: str | None,
                 mood: str | None, style_preferences: list[str], palette_count: int) -> dict[str, Any]:
    brief = {
        "MEASUREMENTS": m.as_dict(),
        "USER_BRIEF": {"room_type": room_type, "desired_mood": mood, "style_preferences": style_preferences},
        "palette_count": palette_count,
    }
    resp = client.messages_create(
        model=MODEL,
        max_tokens=2500,
        system=SYSTEM_PROMPT,
        tools=[REPORT_TOOL],
        tool_choice={"type": "tool", "name": REPORT_TOOL["name"]},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _encode_image(image_bgr)}},
            {"type": "text", "text": "Analyse this room and submit the report.\n" + json.dumps(brief, indent=1)},
        ]}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            return dict(block.input)
    raise RuntimeError("model did not return the report tool call")


# ---------------------------------------------------------------------------
# Curated fallback (and a quality bar the LLM output is compared against)
# ---------------------------------------------------------------------------
CURATED = [
    {"name": "Morning Linen", "tagline": "Soft, sunlit calm", "mood_tags": ["calm", "airy", "restful"],
     "tone": "warm", "psychology": "Pale warm neutrals lower visual noise and help the nervous system settle — ideal where you rest.",
     "colors": [("primary", "#ECE4D6", "Linen"), ("secondary", "#D8CBB6", "Oat"), ("accent", "#9CAF88", "Sage"),
                ("trim", "#F7F4EE", "Chalk"), ("ceiling", "#FBF9F5", "Paper")], "design": "solid"},
    {"name": "Coastal Breath", "tagline": "Fresh air, clear head", "mood_tags": ["fresh", "focused", "light"],
     "tone": "cool", "psychology": "Soft blue-greens are linked with lower heart rate and clearer focus; good for studies and bathrooms.",
     "colors": [("primary", "#D5E1E2", "Sea Mist"), ("secondary", "#9FB7C3", "Harbour"), ("accent", "#2E4A5C", "Deep Tide"),
                ("trim", "#F6F6F2", "Salt"), ("ceiling", "#FFFFFF", "Cloud")], "design": "stripes_vertical"},
    {"name": "Terracotta Supper", "tagline": "Warm, sociable, golden-hour", "mood_tags": ["warm", "sociable", "earthy"],
     "tone": "warm", "psychology": "Earthy reds and clays raise perceived warmth and encourage conversation — great for dining and living.",
     "colors": [("primary", "#EFE6D8", "Plaster"), ("secondary", "#C46A4A", "Terracotta"), ("accent", "#7A4B33", "Clay Pot"),
                ("trim", "#F6F0E6", "Bone"), ("ceiling", "#FAF6EF", "Cream")], "design": "arch"},
    {"name": "Forest Study", "tagline": "Grounded and cocooning", "mood_tags": ["grounded", "natural", "cocooning"],
     "tone": "green", "psychology": "Deep greens read as nature and safety; they reduce eye strain and make large rooms feel intimate.",
     "colors": [("primary", "#3F5E4B", "Forest"), ("secondary", "#8FA382", "Fern"), ("accent", "#C49A6C", "Oak"),
                ("trim", "#E8DFD0", "Parchment"), ("ceiling", "#F3EFE7", "Soft White")], "design": "mountain"},
    {"name": "Midnight Gallery", "tagline": "Dramatic, tailored, cinematic", "mood_tags": ["dramatic", "luxurious", "intimate"],
     "tone": "cool", "psychology": "Inky blues feel protective and sophisticated; art and brass hardware glow against them.",
     "colors": [("primary", "#1F2A44", "Midnight"), ("secondary", "#3A4A68", "Ink"), ("accent", "#B08D57", "Brass"),
                ("trim", "#E9E4DA", "Ivory"), ("ceiling", "#F2EFEA", "Pale Stone")], "design": "two_tone"},
    {"name": "Heather Dusk", "tagline": "Dreamy and gentle", "mood_tags": ["dreamy", "soft", "creative"],
     "tone": "cool", "psychology": "Muted violets balance calm and creativity; the ombré fade softens corners and ceiling lines.",
     "colors": [("primary", "#F4E9DC", "Shell"), ("secondary", "#7A6C9B", "Heather"), ("accent", "#C98C8C", "Rose Clay"),
                ("trim", "#F8F3EC", "Milk"), ("ceiling", "#FBF8F3", "Whisper")], "design": "ombre"},
]


def curated_palettes(m: Measurements, count: int) -> list[dict[str, Any]]:
    """Score curated palettes against the measured light, then take the best distinct ones."""
    def score(p):
        primary = p["colors"][0][1]
        s = 0.0
        if m.color_temperature_k < 3500 and p["tone"] == "cool":
            s -= 2                        # warm bulbs muddy cool greys/blues
        if m.color_temperature_k > 5600 and p["tone"] == "warm":
            s += 0.5                      # cool daylight benefits from warmth
        if m.brightness == "dim" and lrv(primary) < 40:
            s -= 1.5
        if m.brightness == "bright" and lrv(primary) < 20:
            s += 0.5                      # bright rooms can carry drama
        furn_tones = {d["undertone"] for d in m.dominant_colors[:3]}
        if p["tone"] in furn_tones:
            s += 0.7
        return s

    ranked = sorted(CURATED, key=score, reverse=True)
    chosen: list[dict] = []
    for p in ranked:                      # keep directions distinct
        if all(delta_e(p["colors"][0][1], c["colors"][0][1]) > 12 for c in chosen):
            chosen.append(p)
        if len(chosen) == count:
            break
    return [{
        "name": p["name"], "tagline": p["tagline"], "mood_tags": p["mood_tags"], "psychology": p["psychology"],
        "why_it_works": f"Chosen for {m.light_label} light (~{m.color_temperature_k} K) and a {m.brightness} room.",
        "colors": [{"role": r, "hex": h, "label": lbl} for r, h, lbl in p["colors"]],
        "suggested_design": {"pattern": p["design"]},
    } for p in chosen]


# ---------------------------------------------------------------------------
# 3. Validation & enrichment
# ---------------------------------------------------------------------------
PaintMatcher = Callable[[str], dict[str, Any] | None]   # hex → {"paint_color_id","brand","code","name","delta_e"}


def finalize(report: dict[str, Any], m: Measurements, count: int,
             match_paint: PaintMatcher | None = None, _top_up: bool = True) -> dict[str, Any]:
    palettes: list[dict] = []
    for p in report.get("palettes", []):
        colors = []
        for c in p.get("colors", [])[:5]:
            try:
                hx = normalize_hex(c["hex"])
            except (KeyError, ValueError):
                continue
            entry = {"role": c.get("role", "accent"), "hex": hx, "label": str(c.get("label", ""))[:40],
                     "finish": c.get("finish"), "lrv": round(lrv(hx), 1), "undertone": undertone(hx)}
            if match_paint and (match := match_paint(hx)):
                entry.update({k: match.get(k) for k in ("paint_color_id", "brand", "code", "name")})
                entry["match_delta_e"] = round(float(match.get("delta_e", 0)), 2)
            colors.append(entry)
        if len(colors) < 3:
            continue
        primary = next((c for c in colors if c["role"] == "primary"), colors[0])
        if any(delta_e(primary["hex"], q["primary_hex"]) < 8 for q in palettes):
            continue                                            # too similar to an earlier palette
        trim = next((c for c in colors if c["role"] == "trim"), None)
        warnings = []
        if trim and abs(trim["lrv"] - primary["lrv"]) < 10:
            warnings.append("Trim is close to the wall colour — edges may look soft. Choose a crisper trim for definition.")
        if m.color_temperature_k < 3500 and primary["undertone"] == "cool":
            warnings.append(f"Your bulbs are warm (~{m.color_temperature_k} K); this cool colour may look greyer at night. Try it in AR after dark.")
        if m.brightness == "dim" and primary["lrv"] < 25:
            warnings.append("Deep colour in a dim room: lovely and cosy, but it will feel smaller. Balance with a pale ceiling.")
        default_finish = "eggshell" if (report.get("room") or {}).get("type") in ("kitchen", "bathroom", "kids_room") else "matte"
        for c in colors:
            c["finish"] = c["finish"] or ("satin" if c["role"] == "trim" else default_finish)
        palettes.append({
            "name": str(p.get("name", "Palette"))[:60],
            "tagline": str(p.get("tagline", ""))[:120],
            "mood_tags": [str(t).lower()[:24] for t in p.get("mood_tags", [])][:6],
            "psychology": p.get("psychology", ""),
            "why_it_works": p.get("why_it_works", ""),
            "rationale": f"{p.get('psychology', '')} {p.get('why_it_works', '')}".strip(),
            "description": p.get("tagline", ""),
            "colors": colors,
            "suggested_design": p.get("suggested_design") or {"pattern": "solid"},
            "warnings": warnings,
            "primary_hex": primary["hex"],
        })
    # Top up with curated palettes if the model under-delivered or produced duplicates.
    if _top_up and len(palettes) < count:
        extra = finalize({"palettes": curated_palettes(m, len(CURATED)), "room": report.get("room")}, m,
                         len(CURATED), match_paint, _top_up=False)["palettes"]
        for q in extra:
            if len(palettes) >= count:
                break
            if all(delta_e(q["primary_hex"], p["primary_hex"]) >= 8 for p in palettes):
                palettes.append(q)

    return {
        "room_analysis": {
            "summary": report.get("summary", ""),
            "lighting": {"color_temperature_k": m.color_temperature_k, "label": m.light_label,
                         "brightness": m.brightness, "direction": m.light_direction},
            "current_wall": {"hex": m.current_wall_hex, "lrv": m.current_wall_lrv, "undertone": m.current_wall_undertone},
            "dominant_colors": m.dominant_colors,
            "style": report.get("style") or {"detected": "Transitional", "confidence": 0.4},
            "room": report.get("room") or {},
            "constraints": report.get("constraints", []),
        },
        "palettes": palettes[:count],
        "tips": report.get("tips", [])[:4],
    }


# ---------------------------------------------------------------------------
# Entry point used by the AI worker for `palette_recommendation` jobs
# ---------------------------------------------------------------------------
def analyse_room(image_bgr: np.ndarray, *, wall_mask: np.ndarray | None = None, client: LLMClient | None = None,
                 room_type: str | None = None, mood: str | None = None, style_preferences: list[str] | None = None,
                 palette_count: int = 3, match_paint: PaintMatcher | None = None) -> dict[str, Any]:
    m = measure_room(image_bgr, wall_mask)
    report: dict[str, Any] = {"palettes": []}
    source = "curated"
    if client is not None:
        try:
            report = ask_designer(client, image_bgr, m, room_type=room_type, mood=mood,
                                  style_preferences=style_preferences or [], palette_count=palette_count)
            source = "ai"
        except Exception:                       # noqa: BLE001 — never fail the user's request
            log.exception("designer_call_failed_using_curated")
    out = finalize(report, m, palette_count, match_paint)
    out["source"] = source
    out["model_version"] = MODEL if source == "ai" else "curated-v1"
    return out
