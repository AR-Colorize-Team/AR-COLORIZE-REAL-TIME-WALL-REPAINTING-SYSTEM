"""Flicker metric: a perfectly still camera (no jitter) + fresh sensor noise every frame.
Measures how much the painted result changes frame to frame (it should not change at all)."""
import sys, base64, threading, http.server, functools, numpy as np, cv2
from playwright.sync_api import sync_playwright
APP, IMG, TX, TY = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
class Q(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Q, directory="/root/ar")); threading.Thread(target=srv.serve_forever, daemon=True).start()
im = cv2.imread(IMG); H, W = im.shape[:2]; VW = 640; VH = int(round(VW * H / W / 2) * 2)
with sync_playwright() as p:
    br = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
    pg = br.new_page(viewport={"width": VW, "height": VH})
    pg.goto(f"http://127.0.0.1:{srv.server_address[1]}/{APP}/index.html")
    pg.evaluate("""async ([src, vw, vh]) => {
      const im = await new Promise(r => { const i = new Image(); i.onload = () => r(i); i.src = src; });
      window.__c = document.createElement('canvas'); __c.width = vw; __c.height = vh; const g = __c.getContext('2d', { willReadFrequently: true });
      g.drawImage(im, 0, 0, vw, vh); const base = g.getImageData(0, 0, vw, vh);
      let seed = 1; const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
      window.__f = () => { const d = new ImageData(new Uint8ClampedArray(base.data), vw, vh);   // fresh noise, sigma ~ 5 levels
        for (let i = 0; i < d.data.length; i += 4) { const nz = (rnd() + rnd() + rnd() - 1.5) * 10; for (let c = 0; c < 3; c++) d.data[i + c] += nz + (rnd() - 0.5) * 6; }
        g.putImageData(d, 0, 0); };
      __f(); initOnce(); demo = false; startWith(__c, vw, vh); running = false; return true; }""", ["/real/" + IMG.split("/")[-1], VW, VH])
    alphas = []
    for f in range(40):
        b = pg.evaluate("""([f, tx, ty]) => { __f(); try { camFresh = true; } catch (e) {} analyseFrame(); stats.frames++;
          if (f === 2) { applyLook({a: '#E0B65C', nameA: 'P', pattern: 0}); tapAt(tx, ty); }
          walls.forEach(w => w.tap.t = -100); window.__dbg = 2; draw(performance.now());
          const px = new Uint8Array(gl.drawingBufferWidth * gl.drawingBufferHeight * 4);
          gl.readPixels(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight, gl.RGBA, gl.UNSIGNED_BYTE, px);
          const r = new Uint8Array(px.length / 4); for (let i = 0; i < r.length; i++) r[i] = px[i * 4];
          let s = ''; for (let i = 0; i < r.length; i += 0x8000) s += String.fromCharCode.apply(null, r.subarray(i, i + 0x8000));
          return [gl.drawingBufferWidth, gl.drawingBufferHeight, btoa(s)]; }""", [f, TX, TY])
        if f >= 15: alphas.append(np.frombuffer(base64.b64decode(b[2]), np.uint8).reshape(b[1], b[0]).astype(np.float32) / 255)
    print("dmean static", pg.evaluate("() => stats.dmean"))
    A = np.stack(alphas); D = np.abs(np.diff(A, axis=0)); cv2.imwrite(f"/root/ar/real/flk_{APP}.png", np.clip(A.std(0)[::-1] * 1200, 0, 255).astype(np.uint8) if False else np.clip(A.std(0) * 1200, 0, 255).astype(np.uint8))
    flips = ((A[1:] > 0.5) != (A[:-1] > 0.5)).mean()
    painted = (A.mean(0) > 0.5).mean()
    edge = (A.std(0) > 0.08).mean()
    print(f"{APP:10s} {IMG.split('/')[-1]}: mean |Δalpha|/frame {D.mean()*100:.3f}%  pixels flipping/frame {flips*100:.3f}%  flickering px {edge*100:.2f}%  painted {painted*100:.1f}%")
    br.close()
