"""Colour fidelity: the app's repaint vs a ground-truth render of the same room with the wall
really repainted (same light, same camera exposure, tone curve and noise). CIEDE2000 over wall
pixels the app painted (segmentation errors excluded), static camera.

    python color_bench.py <app_dir> [--known]
"""
import sys, base64, json, numpy as np, cv2
APPN = sys.argv[1]; KNOWN = "--known" in sys.argv
sys.argv = ["x", APPN]
exec(open("harness.py").read().split("results = {}")[0])
from playwright.sync_api import sync_playwright

PAINTS = ["#9CAF88", "#1F2A44", "#C46A4A", "#F2EEE6", "#E0B65C", "#7FA1B3"]
def lin2hex(c):
    c = np.array(c); s = np.where(c <= 0.0031308, 12.92 * c, 1.055 * c ** (1 / 2.4) - 0.055)
    return "#" + "".join(f"{int(round(v * 255)):02X}" for v in s)
WALL = {"warm": lin2hex([0.80, 0.77, 0.70]), "lavender": lin2hex([0.66, 0.62, 0.74])}

def srgb2lab(img):
    c = img.astype(np.float64) / 255
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    M = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
    xyz = c @ M.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)

def de2000(l1, l2):
    L1, a1, b1 = l1[..., 0], l1[..., 1], l1[..., 2]; L2, a2, b2 = l2[..., 0], l2[..., 1], l2[..., 2]
    C1, C2 = np.hypot(a1, b1), np.hypot(a2, b2); Cb = (C1 + C2) / 2
    G = 0.5 * (1 - np.sqrt(Cb ** 7 / (Cb ** 7 + 25 ** 7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p, h2p = np.degrees(np.arctan2(b1, a1p)) % 360, np.degrees(np.arctan2(b2, a2p)) % 360
    dL, dC = L2 - L1, C2p - C1p
    dh = h2p - h1p; dh = np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh)); dh = np.where(C1p * C2p == 0, 0, dh)
    dH = 2 * np.sqrt(C1p * C2p) * np.sin(np.radians(dh / 2))
    Lb, Cbp = (L1 + L2) / 2, (C1p + C2p) / 2
    hb = np.where(np.abs(h1p - h2p) > 180, (h1p + h2p + 360) / 2, (h1p + h2p) / 2); hb = np.where(C1p * C2p == 0, h1p + h2p, hb)
    T = 1 - 0.17 * np.cos(np.radians(hb - 30)) + 0.24 * np.cos(np.radians(2 * hb)) + 0.32 * np.cos(np.radians(3 * hb + 6)) - 0.20 * np.cos(np.radians(4 * hb - 63))
    dth = 30 * np.exp(-((hb - 275) / 25) ** 2); RC = 2 * np.sqrt(Cbp ** 7 / (Cbp ** 7 + 25 ** 7))
    SL = 1 + 0.015 * (Lb - 50) ** 2 / np.sqrt(20 + (Lb - 50) ** 2); SC = 1 + 0.045 * Cbp; SH = 1 + 0.015 * Cbp * T
    RT = -np.sin(np.radians(2 * dth)) * RC
    return np.sqrt((dL / SL) ** 2 + (dC / SC) ** 2 + (dH / SH) ** 2 + RT * (dC / SC) * (dH / SH))

def grab(pg, dbg):
    r = pg.evaluate("""(dbg) => { window.__dbg = dbg; draw(performance.now());
      const px = new Uint8Array(gl.drawingBufferWidth * gl.drawingBufferHeight * 4);
      gl.readPixels(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight, gl.RGBA, gl.UNSIGNED_BYTE, px);
      let s = ''; for (let i = 0; i < px.length; i += 0x8000) s += String.fromCharCode.apply(null, px.subarray(i, i + 0x8000));
      return { w: gl.drawingBufferWidth, h: gl.drawingBufferHeight, px: btoa(s) }; }""", dbg)
    return np.frombuffer(base64.b64decode(r["px"]), np.uint8).reshape(r["h"], r["w"], 4)[::-1][..., :3]

T = 2.0
res = {}
with sync_playwright() as p:
    br = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
    for variant in ("warm", "lavender"):
        pg = br.new_page(viewport={"width": VW, "height": VH})
        pg.goto(f"http://127.0.0.1:{PORT}/{APP}/index.html"); pg.evaluate(JS_SETUP, [variant])
        for f in range(12): pg.evaluate(JS_STEP, [T, [1080, 640], f == 0, 2])
        if KNOWN: pg.evaluate("(h) => { wallKnown = h; }", WALL[variant])
        alpha = grab(pg, 2)[..., 0]
        lab = np.frombuffer(base64.b64decode(pg.evaluate(JS_STEP, [T, [1080, 640], False, 2])["lab"]), np.uint8).reshape(VH, VW)
        wallm = cv2.erode((lab == 0).astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
        sel = wallm & (alpha > 242)
        for paint in PAINTS:
            pg.evaluate("(h) => { applyLook({ a: h, nameA: h, pattern: 0, finish: 'matte' }); walls.forEach(w => { w.look = { ...look }; w.tap.t = -100; }); }", paint)
            out = grab(pg, 0)
            gt = pg.evaluate("""async (src) => { const im = await new Promise(r => { const i = new Image(); i.onload = () => r(i); i.src = src; });
                const [x, y, w, h] = __crop(%f); const c = document.createElement('canvas'); c.width = %d; c.height = %d;
                const g = c.getContext('2d'); g.drawImage(im, x, y, w, h, 0, 0, c.width, c.height);
                const e = 0.03 * Math.sin(%f * 0.9); if (e) { g.fillStyle = e > 0 ? `rgba(255,255,255,${e})` : `rgba(0,0,0,${-e})`; g.fillRect(0, 0, c.width, c.height); }
                return c.toDataURL('image/png'); }""" % (T, VW, VH, T), f"/bench/gt_{variant}_{paint[1:]}.png")
            gti = cv2.cvtColor(cv2.imdecode(np.frombuffer(base64.b64decode(gt.split(",")[1]), np.uint8), 1), cv2.COLOR_BGR2RGB)
            # compare on a 3×3-blurred version (sensor noise is independent in the two renders)
            la, lb = srgb2lab(cv2.blur(out, (3, 3))), srgb2lab(cv2.blur(gti, (3, 3)))
            d = de2000(la, lb)[sel]
            dl = (la[..., 0] - lb[..., 0])[sel]
            res[f"{variant} {paint}"] = (float(d.mean()), float(np.percentile(d, 95)), float(dl.mean()), int(sel.sum()))
            if paint in ("#1F2A44", "#C46A4A"):
                cv2.imwrite(f"color_{APPN}{'_known' if KNOWN else ''}_{variant}_{paint[1:]}.png", cv2.cvtColor(np.hstack([out, gti]), cv2.COLOR_RGB2BGR))
        pg.close()
    br.close()
print(f"{APPN} {'known' if KNOWN else 'auto'}:  mean ΔE00 / P95 / ΔL* (painted wall px)")
for k, v in res.items(): print(f"  {k:18s} {v[0]:6.2f} {v[1]:6.2f} {v[2]:+6.2f}  n={v[3]}")
print(f"  {'ALL':18s} {np.mean([v[0] for v in res.values()]):6.2f}")
