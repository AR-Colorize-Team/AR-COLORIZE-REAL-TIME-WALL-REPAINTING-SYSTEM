"""Hard synthetic room for the AR wall tracker, with per-pixel ground truth.

Labels: 0 main wall, 1 ceiling, 2 floor, 3 side wall, 4 window, 5 door,
        6 curtain, 7 small objects (switch, art, skirting, sofa, plant)

The traps are deliberate: the ceiling and the side wall use the SAME paint as
the main wall and meet it through soft, shaded corners; the floor tiles are a
warm beige close to the wall's chromaticity; the window is blown-out sky; the
curtain is a pale fabric. Rendered in linear light, then put through a camera
tone curve, lens blur and sensor noise.

    python make_scene.py <out_dir> <variant>   # variant: warm | lavender
"""
import sys, pathlib
import cv2
import numpy as np

out = pathlib.Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
variant = sys.argv[2] if len(sys.argv) > 2 else "warm"
rng = np.random.default_rng(3 if variant == "warm" else 9)

W, H = 1800, 1050
yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)

if variant == "warm":
    wall_alb = np.array([0.80, 0.77, 0.70], np.float32)      # off-white emulsion
    light = np.array([1.00, 0.86, 0.68], np.float32)        # ~3300 K lamp-ish
    floor_alb = np.array([0.66, 0.58, 0.47], np.float32)    # beige marble
else:
    wall_alb = np.array([0.66, 0.62, 0.74], np.float32)     # lavender
    light = np.array([0.92, 0.95, 1.00], np.float32)        # ~6000 K daylight
    floor_alb = np.array([0.52, 0.50, 0.50], np.float32)    # grey tiles

# ---- geometry (perspective-ish room seen slightly from the left) -----------
ceil_y = 150 + 0.035 * xx                       # wall/ceiling crease (sloped)
floor_y = 880 - 0.02 * xx                       # wall/floor crease
side_x0 = 1480                                  # main/side wall vertical crease
in_side = xx > side_x0
# side wall recedes: its ceiling/floor lines converge
ceil_side = ceil_y + (xx - side_x0) * 0.33
floor_side = floor_y - (xx - side_x0) * 0.30
ceil_line = np.where(in_side, ceil_side, ceil_y)
floor_line = np.where(in_side, floor_side, floor_y)

lab = np.zeros((H, W), np.uint8)
lab[in_side] = 3
lab[yy < ceil_line] = 1
lab[yy >= floor_line] = 2

# ---- shading ----------------------------------------------------------------
win = 0.30 + 0.75 * np.exp(-(((xx - 330) / 620) ** 2))                    # window fall-off
lamp = 0.45 * np.exp(-(((xx - 1150) / 230) ** 2 + ((yy - 330) / 210) ** 2))
sofa_sh = 1 - 0.45 * np.exp(-(((xx - 820) / 360) ** 4 + ((yy - 760) / 110) ** 2))
shade_main = (win + lamp) * sofa_sh
# ambient occlusion along creases: soft darkening on BOTH sides (the hard case)
def ao(dist, depth, width):
    return 1 - depth * np.exp(-np.abs(dist) / width)
d_ceil = yy - ceil_line
d_side = xx - side_x0
shade = shade_main.copy()
shade *= ao(d_ceil, 0.22, 26)
shade *= np.where(lab == 0, ao(d_side, 0.25, 30), 1)
# ceiling: lit from below, a bit dimmer, smooth gradient
ceil_shade = (0.55 + 0.25 * np.exp(-(((xx - 600) / 700) ** 2))) * ao(d_ceil, 0.22, 26)
shade = np.where(lab == 1, ceil_shade, shade)
# side wall: facing the window, darker overall + crease AO
side_shade = (0.42 + 0.10 * np.exp(-((yy - 500) / 400) ** 2)) * ao(d_side, 0.25, 30)
shade = np.where(lab == 3, side_shade * ao(d_ceil, 0.22, 26), shade)
floor_shade = 0.75 * (0.6 + 0.6 * np.exp(-(((xx - 500) / 800) ** 2)))
shade = np.where(lab == 2, floor_shade, shade)

# ---- albedo ------------------------------------------------------------------
plaster = cv2.GaussianBlur(rng.normal(0, 1, (H, W)).astype(np.float32), (0, 0), 1.8) * 0.03
alb = np.empty((H, W, 3), np.float32)
alb[:] = wall_alb
alb *= (1 + plaster)[..., None]
# ceiling: same paint, a touch whiter (realistic: ceiling white vs wall emulsion is often near-identical)
ceil_alb = wall_alb * 0.97 + 0.03
alb[lab == 1] = ceil_alb
# floor: marble tiles with veining and grout lines
tile = 120
fx, fy = xx / tile, (yy - floor_line) / (tile * 0.55)
vein = cv2.GaussianBlur(rng.normal(0, 1, (H, W)).astype(np.float32), (0, 0), 6) * 0.10
tile_id = (np.floor(fx) + 7 * np.floor(fy)) % 5
fl = floor_alb[None, None, :] * (1 + vein[..., None] + 0.04 * (tile_id[..., None] - 2))
grout = (np.abs(fx - np.round(fx)) < 0.02) | (np.abs(fy - np.round(fy)) < 0.035)
fl[grout] = floor_alb * 0.75
alb = np.where((lab == 2)[..., None], fl, alb)

PAINT = sys.argv[3] if len(sys.argv) > 3 else None     # optional: ground-truth repaint of the main wall
if PAINT:
    c = np.array([int(PAINT[i:i + 2], 16) / 255 for i in (1, 3, 5)], np.float32)
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    alb = np.where((lab == 0)[..., None], c[None, None, :] * (1 + plaster)[..., None], alb)
lin = alb * shade[..., None] * light

def rect(x0, y0, x1, y1, color=None, label=None, value=None):
    sl = (slice(int(y0), int(y1)), slice(int(x0), int(x1)))
    if value is not None: lin[sl] = value
    elif color is not None: lin[sl] = np.array(color, np.float32) * light * 0.9
    if label is not None: lab[sl] = label

# window (left of main wall): white frame + blown-out sky with tree silhouettes
rect(120, 260, 560, 760, color=[0.88, 0.88, 0.86], label=4)
sky = np.zeros((480, 400, 3), np.float32)
sy = np.linspace(0, 1, 480)[:, None]
sky[:] = (np.array([1.9, 2.1, 2.6]) * (1 - 0.3 * sy[..., None]))
trees = cv2.GaussianBlur((rng.random((480, 400)) > 0.993).astype(np.float32), (0, 0), 14)
trees = np.clip(trees * 120, 0, 1) * (sy > 0.55)
sky = sky * (1 - trees[..., None]) + np.array([0.08, 0.12, 0.06]) * trees[..., None]
lin[270:750, 140:540] = sky
lin[270:750, 333:347] = np.array([0.85, 0.85, 0.83]) * light * 0.9   # mullion
# curtain beside the window: pale fabric with vertical folds
cx = np.arange(570, 700)
folds = 0.75 + 0.25 * np.sin((cx - 570) / 9.0)
cur = np.array([0.82, 0.74, 0.62], np.float32) * light
for i, x in enumerate(cx):
    lin[200:860, x] = cur * folds[i] * (0.95 * win[500, x])
lab[200:860, 570:700] = 6
# door on the main wall
rect(1250, 330, 1440, 900, color=[0.92, 0.92, 0.90], label=5)      # frame
rect(1262, 342, 1428, 900, color=[0.36, 0.22, 0.13], label=5)      # wood
rect(1395, 620, 1415, 634, color=[0.8, 0.75, 0.6], label=5)        # handle
# switchboard (white on a pale wall: the classic trap)
rect(1150, 520, 1190, 580, color=[0.93, 0.93, 0.91], label=7)
rect(1162, 535, 1178, 565, color=[0.86, 0.86, 0.84], label=7)
# framed art
rect(760, 300, 1020, 470, color=[0.9, 0.88, 0.84], label=7)
rect(772, 312, 1008, 458, color=[0.25, 0.36, 0.42], label=7)
# skirting board along the floor crease (main wall)
for x in range(0, side_x0):
    fy0 = int(floor_y[0, x]); lin[fy0 - 22:fy0, x] = np.array([0.9, 0.9, 0.88]) * light * 0.7
    lab[fy0 - 22:fy0, x] = 7
# sofa + cushions + plant
rect(560, 700, 1110, 960, color=[0.16, 0.17, 0.19], label=7)
rect(600, 650, 760, 740, color=[0.62, 0.55, 0.42], label=7)
rect(920, 655, 1070, 740, color=[0.45, 0.52, 0.42], label=7)
cv2.circle(lab, (1185, 760), 75, 7, -1)
m = np.zeros((H, W), np.uint8); cv2.circle(m, (1185, 760), 75, 1, -1)
lin[m > 0] = np.array([0.10, 0.20, 0.09]) * light
rect(1150, 830, 1220, 960, color=[0.30, 0.20, 0.14], label=7)

# ---- camera: lens blur, exposure, tone curve, noise ------------------------
lin = cv2.GaussianBlur(lin, (0, 0), 0.8)
base = (np.where((lab == 0)[..., None], (wall_alb * (1 + plaster)[..., None]) * shade[..., None] * light, lin)) if PAINT else lin
expo = 0.42 / np.median(cv2.GaussianBlur(base, (0, 0), 0.8)[lab == 0] @ np.array([0.2126, 0.7152, 0.0722], np.float32))
x = lin * expo
x = x * (1 + 0.6 * x) / (1 + x)              # gentle highlight shoulder
x = np.clip(x, 0, 1)
srgb = np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1 / 2.4) - 0.055)
srgb = srgb + rng.normal(0, 3.5 / 255, srgb.shape)
img = np.clip(np.round(srgb * 255), 0, 255).astype(np.uint8)
if PAINT:
    cv2.imwrite(str(out / f"gt_{variant}_{PAINT[1:]}.png"), cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
else:
    cv2.imwrite(str(out / f"scene_{variant}.png"), cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
    cv2.imwrite(str(out / f"labels_{variant}.png"), lab)
print("wrote", out, img.shape, np.bincount(lab.ravel(), minlength=8))
