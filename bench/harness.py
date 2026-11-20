"""Drive live/index.html frame by frame in headless Chrome and score the painted mask
against ground-truth labels.

    python harness.py <app_dir_name> [--shots]

The app is served from /root/ar/<app_dir_name>/index.html. A moving virtual camera
pans over bench/scene_<variant>.png. Each scenario taps a point, then runs N frames.
Scoring is done on the FINAL shader output at full resolution (debug mode 2 renders
wall-0 paint alpha as grey), so the numbers include upsampling and edge tests.
"""
import json, sys, base64, threading, http.server, functools, pathlib
import numpy as np, cv2
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path("/root/ar")
APP = sys.argv[1]
SHOTS = "--shots" in sys.argv
VW, VH = 640, 360
NAMES = ["wall", "ceiling", "floor", "side_wall", "window", "door", "curtain", "objects"]

class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT)))
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]

# scenarios: (name, variant, tap point in SCENE pixels, expected label, frames)
SCEN = [
    ("main_wall_A", "warm", (1080, 640), 0, 36),
    ("main_wall_B", "warm", (900, 235), 0, 36),
    ("main_wall_lav", "lavender", (1080, 640), 0, 36),
    ("main_wall_lav_B", "lavender", (420, 815), 0, 36),
    ("side_wall", "warm", (1640, 560), 3, 24),
    ("tap_window_sky", "warm", (230, 400), None, 16),
    ("tap_window_trees", "warm", (250, 680), None, 16),
    ("tap_window_lav", "lavender", (440, 380), None, 16),
]

JS_SETUP = r"""
async ([variant]) => {
  const load = src => new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = src; });
  window.__scene = await load(`/bench/scene_${variant}.png`);
  window.__labels = await load(`/bench/labels_${variant}.png`);
  window.__cam = document.createElement('canvas'); __cam.width = %d; __cam.height = %d;
  window.__lab = document.createElement('canvas'); __lab.width = %d; __lab.height = %d;
  window.__cctx = __cam.getContext('2d'); window.__lctx = __lab.getContext('2d', { willReadFrequently: true });
  window.__crop = (t) => {           // virtual handheld camera: pan + zoom + exposure breathing
    const zoom = 1 + 0.07 * Math.sin(t * 0.31);
    const w = 1600 / zoom, h = w * %d / %d;
    const x = 100 + 100 * Math.sin(t * 0.37), y = 1050 - h - 40 - 35 * Math.sin(t * 0.53 + 1);
    return [x, Math.max(0, Math.min(1050 - h, y)), w, h];
  };
  window.__render = (t) => {
    const [x, y, w, h] = __crop(t);
    __cctx.imageSmoothingEnabled = true; __cctx.drawImage(__scene, x, y, w, h, 0, 0, __cam.width, __cam.height);
    const e = 0.03 * Math.sin(t * 0.9);
    if (e) { __cctx.fillStyle = e > 0 ? `rgba(255,255,255,${e})` : `rgba(0,0,0,${-e})`; __cctx.fillRect(0, 0, __cam.width, __cam.height); }
    __lctx.imageSmoothingEnabled = false; __lctx.drawImage(__labels, x, y, w, h, 0, 0, __lab.width, __lab.height);
  };
  window.__dbg = 2;
  initOnce();
  demo = false; startWith(__cam, __cam.width, __cam.height); running = false; demo = true;   // we drive frames ourselves (no auto-tap)
  return true;
}
""" % (VW, VH, VW, VH, VH, VW)

JS_STEP = r"""
([t, tapScene, doTap, dbg]) => {
  __render(t);
  try { camFresh = true; } catch (e) {}
  if (doTap) {
    const [x, y, w, h] = __crop(t);
    const u = (tapScene[0] - x) / w, v = (tapScene[1] - y) / h;
    analyseFrame(); stats.frames++;
    tapAt(u, v);
    if (walls[0] && walls[0].tap) walls[0].tap.t = -100;   // skip the reveal ripple
  }
  analyseFrame(); stats.frames++;
  if (walls[0] && walls[0].tap) walls[0].tap.t = -100;
  window.__dbg = dbg;
  draw(performance.now());
  const px = new Uint8Array(gl.drawingBufferWidth * gl.drawingBufferHeight * 4);
  gl.readPixels(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight, gl.RGBA, gl.UNSIGNED_BYTE, px);
  const lab = __lctx.getImageData(0, 0, __lab.width, __lab.height).data;
  const b64 = (u8) => { let s = ''; for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000)); return btoa(s); };
  const full = dbg !== 2;
  const r1 = new Uint8Array(px.length / 4), l1 = new Uint8Array(lab.length / 4);
  for (let i = 0; i < r1.length; i++) r1[i] = px[i * 4];
  for (let i = 0; i < l1.length; i++) l1[i] = lab[i * 4];
  return { w: gl.drawingBufferWidth, h: gl.drawingBufferHeight, px: full ? b64(px) : b64(r1), lab: b64(l1), full,
           nwalls: walls.length, toast: document.getElementById('toast').hidden ? '' : document.getElementById('toast').textContent,
           trackMs: stats.trackMs };
}
"""

def boundary(mask):
    k = np.ones((3, 3), np.uint8)
    return (cv2.dilate(mask, k) - cv2.erode(mask, k)) > 0

def bf1(pred, gt, tol=2):
    bp, bg = boundary(pred), boundary(gt)
    if bp.sum() == 0 or bg.sum() == 0: return 0.0
    k = np.ones((2 * tol + 1, 2 * tol + 1), np.uint8)
    prec = (bp & (cv2.dilate(bg.astype(np.uint8), k) > 0)).sum() / bp.sum()
    rec = (bg & (cv2.dilate(bp.astype(np.uint8), k) > 0)).sum() / bg.sum()
    return 2 * prec * rec / max(1e-9, prec + rec)

results = {}
shotdir = ROOT / "bench" / f"shots_{APP}"; shotdir.mkdir(exist_ok=True)
with sync_playwright() as p:
    br = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])
    for name, variant, tap, expect, nframes in SCEN:
        pg = br.new_page(viewport={"width": VW, "height": VH}, device_scale_factor=1)
        errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(f"http://127.0.0.1:{PORT}/{APP}/index.html")
        pg.evaluate(JS_SETUP, [variant])
        rows, toasts, nw = [], set(), 0
        for f in range(nframes):
            t = 2.0 + f * 0.12
            r = pg.evaluate(JS_STEP, [t, list(tap), f == 0, 2])
            if r["toast"]: toasts.add(r["toast"])
            nw = r["nwalls"]
            img = np.frombuffer(base64.b64decode(r["px"]), np.uint8).reshape(r["h"], r["w"])[::-1]
            alpha = img.astype(np.float32) / 255
            if alpha.shape != (VH, VW): alpha = cv2.resize(alpha, (VW, VH), interpolation=cv2.INTER_LINEAR)
            lab = np.frombuffer(base64.b64decode(r["lab"]), np.uint8).reshape(VH, VW)
            pred = (alpha > 0.5).astype(np.uint8)
            if f >= 4:
                row = {}
                for k, nm in enumerate(NAMES):
                    cnt = (lab == k).sum()
                    row[nm] = float(pred[lab == k].mean()) if cnt else None
                if expect is not None:
                    gt = (lab == expect).astype(np.uint8)
                    inter, uni = (pred & gt).sum(), (pred | gt).sum()
                    row["iou"] = inter / max(1, uni)
                    row["spill"] = float(pred[gt == 0].sum()) / max(1, pred.sum())
                    row["bf1"] = bf1(pred, gt)
                row["painted_total"] = float(pred.mean())
                row["trackMs"] = r["trackMs"]
                rows.append(row)
            if SHOTS and f in (nframes - 1,):
                for look, dbg in (("sage", 0), ("navy", 0)):
                    if look == "navy": pg.evaluate("() => { applyLook({a:'#1F2A44', nameA:'Midnight'}); }")
                    else: pg.evaluate("() => { applyLook({a:'#9CAF88', nameA:'Sage'}); }")
                    rr = pg.evaluate(JS_STEP, [t, list(tap), False, 0])
                    im = np.frombuffer(base64.b64decode(rr["px"]), np.uint8).reshape(rr["h"], rr["w"], 4)[::-1][..., :3]
                    cv2.imwrite(str(shotdir / f"{name}_{look}.png"), cv2.cvtColor(im, cv2.COLOR_RGB2BGR))
                cam = pg.evaluate("() => __cam.toDataURL('image/png')")
                import base64; (shotdir / f"{name}_cam.png").write_bytes(base64.b64decode(cam.split(",")[1]))
                cv2.imwrite(str(shotdir / f"{name}_mask.png"), (alpha * 255).astype(np.uint8))
        agg = {}
        for key in rows[0]:
            vals = [r[key] for r in rows if r[key] is not None]
            agg[key] = round(float(np.mean(vals)), 4) if vals else None
        agg["walls_after"] = nw; agg["toasts"] = sorted(toasts); agg["errors"] = errs[:3]
        results[name] = agg
        pg.close()
    br.close()

print(json.dumps(results, indent=1))
(ROOT / "bench" / f"results_{APP}.json").write_text(json.dumps(results, indent=1))
