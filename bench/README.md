# Evaluation bench

Reproducible tests for the live web app (`../web/index.html`). Requirements: Python 3, OpenCV, NumPy and Playwright with Chrome.

| Script | What it measures |
|---|---|
| `make_scene.py` | Builds a hard synthetic room with per-pixel ground truth (same-paint ceiling and side wall with soft corners, beige floor, blown-out window, curtain, door, switch, furniture, sensor noise, tone curve). With a paint colour as the third argument it renders the *true* repaint. |
| `harness.py` | Drives the app frame by frame in headless Chrome with a moving camera, and scores the final shader output against the labels: spill per surface, IoU, boundary F1, window-tap refusal. |
| `color_bench.py` | CIEDE2000 colour error of the preview against the true repaint (`--known` uses *Wall today*). |
| `flicker.py` | A still camera with fresh sensor noise every frame: how much the painted result changes frame to frame. |
| `summarize.py` | Prints the result table. |

```bash
python make_scene.py . warm && python make_scene.py . lavender
python harness.py <app_dir> && python summarize.py <app_dir>
```
`harness.py` serves its parent folder and loads `<app_dir>/index.html`; place a copy of `web/` next to `bench/` (or adjust `ROOT`).
