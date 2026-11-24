"""
Synthetic room used to exercise the paint renderer: a plastered wall lit by a
window on the left and a lamp, a sofa casting a soft shadow, a door, a window
and a switchboard (all of which must stay unpainted).

    python demo_scene.py            # writes previews/paint_preview.png
"""
from __future__ import annotations

import pathlib

import cv2
import numpy as np

from paint_math import estimate_light_tint, pattern_paint, recolor

W, H = 960, 640
WALL_W_M, WALL_H_M = 4.0, 2.6
FLOOR_Y = 560                      # pixel row where the wall meets the floor


def build_scene(seed: int = 7):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    img = np.zeros((H, W, 3), np.float32)

    # Wall albedo (warm off-white) with plaster texture
    wall = np.array([0.86, 0.84, 0.80], np.float32)
    plaster = cv2.GaussianBlur(rng.normal(0, 1, (H, W)).astype(np.float32), (0, 0), 1.6) * 0.025
    # Lighting: window light from the left + lamp hotspot + overall falloff to the right
    window_light = 0.55 + 0.55 * np.exp(-((xx - 40) / 420) ** 2)
    lamp = 0.35 * np.exp(-(((xx - 640) / 140) ** 2 + ((yy - 200) / 120) ** 2))
    sofa_shadow = 1 - 0.45 * np.exp(-(((xx - 470) / 260) ** 4 + ((yy - 470) / 70) ** 2))
    light = (window_light + lamp) * sofa_shadow
    img[:] = wall
    img *= (light + plaster)[..., None] * np.array([1.0, 0.97, 0.90])  # warm daylight + bulb

    mask = np.ones((H, W), np.float32)
    mask[FLOOR_Y:] = 0

    # Floor
    img[FLOOR_Y:] = np.array([0.45, 0.30, 0.20]) * (0.8 + 0.2 * np.sin(xx[FLOOR_Y:] / 9))[..., None]
    # Skirting board
    img[FLOOR_Y - 14:FLOOR_Y] = 0.93; mask[FLOOR_Y - 14:FLOOR_Y] = 0
    # Window (left)
    x0, x1, y0, y1 = 70, 300, 90, 380
    img[y0:y1, x0:x1] = np.array([0.62, 0.78, 0.95]); mask[y0 - 8:y1 + 8, x0 - 8:x1 + 8] = 0
    img[y0 - 8:y0, x0 - 8:x1 + 8] = img[y1:y1 + 8, x0 - 8:x1 + 8] = 0.95
    img[y0:y1, x0 - 8:x0] = img[y0:y1, x1:x1 + 8] = 0.95
    img[y0:y1, (x0 + x1) // 2 - 3:(x0 + x1) // 2 + 3] = 0.95
    # Door (right)
    dx0, dx1, dy0 = 790, 920, 200
    img[dy0:FLOOR_Y, dx0:dx1] = np.array([0.52, 0.36, 0.24]); mask[dy0 - 6:FLOOR_Y, dx0 - 6:dx1 + 6] = 0
    img[dy0 + 180:dy0 + 190, dx0 + 15:dx0 + 30] = 0.75   # handle
    # Switchboard
    img[300:340, 745:770] = 0.97; mask[300:340, 745:770] = 0
    # Sofa (foreground object)
    sofa = (np.abs(xx - 470) < 230) & (yy > 430)
    img[sofa] = np.array([0.22, 0.24, 0.27]); mask[sofa] = 0

    img = np.clip(img, 0, 1)
    # Soft edges, as the on-device segmenter produces (bilinear upsample of a low-res mask)
    mask = cv2.GaussianBlur(mask, (0, 0), 1.2)
    # Wall-plane coordinates in metres (fronto-parallel here; in the app they come from the AR plane)
    u = xx / W * WALL_W_M
    v = (FLOOR_Y - yy) / FLOOR_Y * WALL_H_M
    return img, mask, u, v


LOOKS = [
    ("Original",                    None),
    ("Sage — matte",                dict(pattern="solid", a="#9CAF88", b="#9CAF88", finish="matte")),
    ("Midnight navy — eggshell",    dict(pattern="solid", a="#1F2A44", b="#1F2A44", finish="eggshell")),
    ("Two-tone: linen / terracotta", dict(pattern="two_tone", a="#EFE6D8", b="#C46A4A", finish="matte")),
    ("Arch: blush on cream",        dict(pattern="arch", a="#F3ECE2", b="#D8A7A0", finish="matte",
                                         params={"center_m": 2.35, "width_m": 1.5, "height_m": 2.2})),
    ("Mountain: sand / forest",     dict(pattern="mountain", a="#E8DFD0", b="#3F5E4B", finish="matte")),
    ("Vertical stripes",            dict(pattern="stripes_vertical", a="#F2EEE6", b="#B8C5D6", finish="satin",
                                         params={"stripe_m": 0.25})),
    ("Ombre: dusk",                 dict(pattern="ombre", a="#F4E9DC", b="#7A6C9B", finish="matte")),
]


def render_look(img, mask, u, v, look):
    if look is None:
        return img
    paint = pattern_paint(look["pattern"], u, v, look["a"], look["b"], look.get("params"),
                          px=WALL_W_M / W)
    tint = estimate_light_tint(img, mask, old_wall_hex="#DBD6CC")
    return recolor(img, mask, paint, finish=look["finish"], light_tint=tint)


def main():
    img, mask, u, v = build_scene()
    tiles = []
    for title, look in LOOKS:
        out = render_look(img, mask, u, v, look)
        tile = (np.clip(out, 0, 1) * 255).astype(np.uint8)
        tile = cv2.resize(tile, (480, 320), interpolation=cv2.INTER_AREA)
        bar = np.full((36, 480, 3), 250, np.uint8)
        cv2.putText(bar, title, (12, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (30, 30, 30), 1, cv2.LINE_AA)
        tiles.append(np.vstack([bar, tile]))
    rows = [np.hstack(tiles[i:i + 2]) for i in range(0, len(tiles), 2)]
    grid = np.vstack(rows)
    out_dir = pathlib.Path(__file__).parent / "previews"
    out_dir.mkdir(exist_ok=True)
    cv2.imwrite(str(out_dir / "paint_preview.png"), cv2.cvtColor(grid, cv2.COLOR_RGB2BGR))
    print("wrote", out_dir / "paint_preview.png", grid.shape)


if __name__ == "__main__":
    main()
