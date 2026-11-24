"""Behavioural tests for the paint renderer (the shaders must satisfy the same properties)."""
import numpy as np
import pytest

from demo_scene import FLOOR_Y, W, build_scene
from paint_math import (LUMA, PATTERNS, hex_to_linear, linear_to_srgb, pattern_weights, recolor,
                        srgb_to_linear)


@pytest.fixture(scope="module")
def scene():
    return build_scene()


def lum(img):
    return srgb_to_linear(img) @ LUMA


def test_srgb_roundtrip():
    x = np.linspace(0, 1, 1001, dtype=np.float32)
    assert np.allclose(linear_to_srgb(srgb_to_linear(x)), x, atol=1e-5)


def test_non_wall_pixels_untouched(scene):
    img, mask, *_ = scene
    out = recolor(img, mask, hex_to_linear("#1F2A44"))
    hard = mask < 1e-3
    assert np.array_equal(out[hard], img[hard]), "doors/windows/switches/sofa must be pixel-identical"


def test_shadows_and_light_falloff_are_preserved(scene):
    """Ratio between a shadowed and a lit wall pixel survives the repaint (lighting is kept)."""
    img, mask, *_ = scene
    lit, shadow = (200, 600), (415, 470)          # (row, col): bright wall vs. sofa-shadowed wall
    assert mask[lit] > 0.99 and mask[shadow] > 0.99
    ratio_before = lum(img)[shadow] / lum(img)[lit]
    out = recolor(img, mask, hex_to_linear("#9CAF88"), finish="flat")
    ratio_after = lum(out)[shadow] / lum(out)[lit]
    assert ratio_after == pytest.approx(ratio_before, rel=0.03)


def test_dark_paint_reads_darker_than_light_paint(scene):
    img, mask, *_ = scene
    wall = mask > 0.99
    navy = lum(recolor(img, mask, hex_to_linear("#1F2A44")))[wall].mean()
    sage = lum(recolor(img, mask, hex_to_linear("#9CAF88")))[wall].mean()
    white = lum(recolor(img, mask, hex_to_linear("#F7F5F0")))[wall].mean()
    assert navy < sage < white
    assert navy < 0.25 * white, "navy must look genuinely dark, not just tinted"


def test_hue_matches_the_paint(scene):
    img, mask, *_ = scene
    out = recolor(img, mask, hex_to_linear("#C46A4A"))            # terracotta: red > green > blue
    r, g, b = srgb_to_linear(out)[mask > 0.99].mean(axis=0)
    assert r > g > b


def test_glossier_finish_is_never_darker(scene):
    img, mask, *_ = scene
    flat = lum(recolor(img, mask, hex_to_linear("#7A8B99"), finish="flat"))
    gloss = lum(recolor(img, mask, hex_to_linear("#7A8B99"), finish="gloss"))
    assert np.all(gloss >= flat - 1e-6)


def test_opacity_blends_linearly(scene):
    img, mask, *_ = scene
    full = recolor(img, mask, hex_to_linear("#9CAF88"), opacity=1.0)
    half = recolor(img, mask, hex_to_linear("#9CAF88"), opacity=0.5)
    on = mask > 0.01   # below ~1/512 coverage the shader early-outs by design
    assert np.allclose(half[on], ((img + full) / 2)[on], atol=1e-5)


@pytest.mark.parametrize("pattern", PATTERNS)
def test_patterns_are_bounded_and_antialiased(pattern):
    u, v = np.meshgrid(np.linspace(0, 4, 800), np.linspace(0, 2.6, 520))
    w = pattern_weights(pattern, u, v, {}, px=4 / 800)
    assert w.min() >= 0 and w.max() <= 1
    if pattern not in ("solid", "ombre"):
        assert (w > 0.99).any() and (w < 0.01).any(), "pattern must actually show both colours"
        frac_edge = ((w > 0.05) & (w < 0.95)).mean()
        assert frac_edge < 0.08, "edges must be crisp (only a ~1px anti-aliased band)"


def test_patterns_are_world_locked():
    """Same wall point → same colour, whatever the pixel grid (camera moved/zoomed)."""
    pts_u, pts_v = np.array([0.3, 1.1, 2.7]), np.array([0.5, 1.2, 2.0])
    for p in PATTERNS:
        a = pattern_weights(p, pts_u, pts_v, {}, px=0.002)
        b = pattern_weights(p, pts_u, pts_v, {}, px=0.002)
        assert np.array_equal(a, b)


def test_wall_extent_sanity():
    img, mask, u, v = build_scene()
    assert u.max() < 4.0 and v[FLOOR_Y - 1, 0] >= 0 and mask[:, :W].shape == mask.shape
