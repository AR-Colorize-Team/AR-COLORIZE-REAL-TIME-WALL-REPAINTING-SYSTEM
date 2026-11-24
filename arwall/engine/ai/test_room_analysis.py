"""Tests for the premium room analysis (no network: the LLM is faked)."""
import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reference"))
from demo_scene import build_scene                       # noqa: E402

from room_analysis import (analyse_room, delta_e, lrv, measure_room, normalize_hex,  # noqa: E402
                           undertone)


def _bgr(img_rgb01):
    return (np.clip(img_rgb01, 0, 1) * 255).astype(np.uint8)[..., ::-1].copy()


def _kelvin_rgb(k):
    t = k / 100
    r = 255 if t <= 66 else 329.698727446 * (t - 60) ** -0.1332047592
    g = 99.4708025861 * np.log(t) - 161.1195681661 if t <= 66 else 288.1221695283 * (t - 60) ** -0.0755148492
    b = 255 if t >= 66 else (0 if t <= 19 else 138.5177312231 * np.log(t - 10) - 305.0447927307)
    return np.clip(np.array([r, g, b]) / 255, 0, 1)


def _lit_scene(kelvin):
    img, mask, *_ = build_scene()
    lin = np.where(img <= 0.04045, img / 12.92, ((img + 0.055) / 1.055) ** 2.4)
    tint = _kelvin_rgb(kelvin) / _kelvin_rgb(6500)
    tint = np.where(tint <= 0.04045, tint / 12.92, ((tint + 0.055) / 1.055) ** 2.4)
    lin = lin * tint / tint.max()
    out = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * np.power(np.clip(lin, 0, 1), 1 / 2.4) - 0.055)
    return _bgr(out), mask


def test_color_basics():
    assert normalize_hex("abc123") == "#ABC123"
    assert lrv("#FFFFFF") == pytest.approx(100, abs=0.01) and lrv("#000000") == 0
    assert delta_e("#9CAF88", "#9CAF88") == 0
    assert undertone("#C46A4A") == "warm" and undertone("#2E3F5C") == "cool" and undertone("#808080") == "neutral"
    assert undertone("#3F5E4B") == "green"


def test_warm_light_measures_warmer_than_daylight():
    warm, mask = _lit_scene(2900)
    cool, _ = _lit_scene(6500)
    mw, mc = measure_room(warm, mask), measure_room(cool, mask)
    assert mw.color_temperature_k < mc.color_temperature_k - 1000
    assert mw.light_label.startswith("warm")


def test_light_direction_and_wall_colour():
    img, mask = _lit_scene(5000)
    m = measure_room(img, mask)
    assert m.light_direction == "left"                    # the scene's window is on the left
    # Absolute LRV from one photo is exposure-limited; the white-anchor gets the right ballpark and tone.
    assert m.current_wall_hex and 30 < m.current_wall_lrv < 90 and m.current_wall_undertone != "cool"
    assert len(m.dominant_colors) >= 3
    assert any(d["name"] in ("charcoal", "black", "grey") for d in m.dominant_colors)   # the sofa


class FakeClient:
    def __init__(self, report=None, fail=False):
        self.report, self.fail, self.calls = report, fail, []

    def messages_create(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise TimeoutError("upstream timeout")
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=self.report)])


GOOD = {
    "summary": "A softly lit living room with warm oak and a charcoal sofa.",
    "style": {"detected": "Japandi", "confidence": 0.8},
    "room": {"type": "living_room", "size": "medium", "est_ceiling_height_m": 2.6},
    "constraints": ["Warm bulbs"],
    "palettes": [
        {"name": "Oat & Olive", "tagline": "Calm", "mood_tags": ["Calm"], "psychology": "p", "why_it_works": "w",
         "colors": [{"role": "primary", "hex": "#e6ddcf", "label": "Oat"}, {"role": "accent", "hex": "#6E7F64", "label": "Olive"},
                    {"role": "trim", "hex": "#E9E1D4", "label": "Too close"}], "suggested_design": {"pattern": "two_tone"}},
        {"name": "Same again", "tagline": "dupe", "mood_tags": [], "psychology": "", "why_it_works": "",
         "colors": [{"role": "primary", "hex": "#E7DED0", "label": "Oat 2"}, {"role": "trim", "hex": "#FFFFFF", "label": "W"},
                    {"role": "ceiling", "hex": "#FFFFFF", "label": "W"}], "suggested_design": {"pattern": "solid"}},
        {"name": "Bad hex", "tagline": "", "mood_tags": [], "psychology": "", "why_it_works": "",
         "colors": [{"role": "primary", "hex": "not-a-colour", "label": "?"}], "suggested_design": {"pattern": "solid"}},
    ],
    "tips": ["Sample at night"],
}


def test_llm_report_is_validated_deduplicated_and_topped_up():
    img, mask = _lit_scene(2900)
    client = FakeClient(GOOD)
    out = analyse_room(img, wall_mask=mask, client=client, palette_count=3,
                       match_paint=lambda hx: {"paint_color_id": "p-1", "brand": "Demo", "code": "D1", "name": "Match", "delta_e": 1.2})
    assert out["source"] == "ai"
    req = client.calls[0]
    assert req["tool_choice"]["name"] == "submit_room_report" and req["messages"][0]["content"][0]["type"] == "image"
    pals = out["palettes"]
    assert len(pals) == 3, "duplicate + invalid palettes dropped, curated ones fill the gap"
    assert pals[0]["name"] == "Oat & Olive" and pals[0]["colors"][0]["hex"] == "#E6DDCF"
    assert any("Trim is close" in w for w in pals[0]["warnings"])
    primaries = [p["primary_hex"] for p in pals]
    assert all(delta_e(a, b) >= 8 for i, a in enumerate(primaries) for b in primaries[i + 1:])
    assert pals[0]["colors"][0]["paint_color_id"] == "p-1" and pals[0]["colors"][0]["finish"] == "matte"
    assert out["room_analysis"]["lighting"]["color_temperature_k"] < 3600


def test_llm_failure_falls_back_to_curated_and_respects_light():
    img, mask = _lit_scene(2700)
    out = analyse_room(img, wall_mask=mask, client=FakeClient(fail=True), palette_count=3)
    assert out["source"] == "curated" and len(out["palettes"]) == 3
    # Under warm light the cool palettes are ranked down
    assert out["palettes"][0]["colors"][0]["undertone"] != "cool"
