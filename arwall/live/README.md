# AR Wall Live — browser build (works today, no app install)

`index.html` is a self-contained live-camera wall painter. Open it on a phone over **HTTPS**, tap
**Start camera**, tap a wall, swipe colours. No build step and no dependencies; the only external
request is Google Fonts, which has a system-font fallback.

## Run it

| Where | How |
|---|---|
| Your laptop | `cd live && python3 -m http.server 8080` → open `http://localhost:8080` (localhost is allowed camera access) |
| Your phone, fastest | Drag the `live/` folder onto **Netlify Drop** (app.netlify.com/drop), or push it to a GitHub repo with **Pages** enabled. You get an `https://` link to open on any phone. |
| Phone on your Wi-Fi | Phones refuse the camera on plain `http://192.168…`. Use an HTTPS tunnel instead (e.g. `npx localtunnel --port 8080` or `cloudflared tunnel --url http://localhost:8080`). |

URL flags for engineers: `?demo` opens the moving demo room (no camera needed), and `?eng` turns on
engineer mode (frame rate, tracking ms, mask coverage, noise floor). In-app: **Settings → Engineer mode / Show detected wall mask**.

Supported: Chrome/Edge/Samsung Internet on Android 9+, Safari on iOS 15+, desktop Chrome/Safari/Firefox (WebGL 1 + `getUserMedia`).

## How a frame is processed

```
camera frame ──► 2D canvas downscale (≤160 px long side) ──► TRACK (JS, ~3–5 ms)
                                                              │  per tapped wall:
                                                              │  • seeds = last frame's wall pixels that still match
                                                              │  • region-grow; stops at colour change / edges
                                                              │    (edge threshold adapts to the camera's noise floor)
                                                              │  • fill pinholes ≤6 px (switchboards are bigger, stay unpainted)
                                                              │  • grow 3 px of "candidate" edge pixels
                                                              │  • temporal smoothing, white-balance drift follow,
                                                              │    median wall brightness, straight floor-line fit
                                                              ▼
camera frame ──► WebGL texture ──► PAINT shader (full res, every display frame)
                                   • core mask → painted; candidates → painted only if the
                                     full-res pixel matches wall colour AND local wall brightness
                                   • recolour = paint albedo × observed shading ÷ wall brightness
                                     (same model as engine/reference/paint_math.py: shadows,
                                     lamp glow and texture stay real; dark paint reads dark)
                                   • designs in metres from the floor line; reveal ripple on tap
```

Rendering doesn't wait for tracking. If the frame rate drops below 24 fps for about two seconds,
render resolution steps down (to as low as 0.5×). If tracking costs more than 14 ms, tracking
resolution drops to 128 px.

## Verified

The app ran in headless Chromium with simulated moving cameras (portrait 360×640 with sensor
noise, and the built-in demo room) and these were checked:
* the wall is detected from one tap and tracked while the camera pans;
* the door, window, art frame, switchboard, plant and sofa stay unpainted;
* two-tone follows the floor line behind the sofa;
* compare, Room read, undo, hold and engineer stats all work;
* no page errors.

The tracking CPU cost measured 3–5 ms per frame. The test machine has no GPU, so the browser drew
every pixel in software and frame rates there were 4–17 fps. That number is not representative of
phones, which run this shader on the GPU. **It has not yet been tried on a physical phone.**

## Known limits of the browser build (and what the native app adds)

| Browser build (this) | Native Flutter app (`app/`) |
|---|---|
| Colour-and-edge wall tracking: great on plain walls, weaker on heavy texture or wallpaper, and on a wall the same colour as the ceiling | On-device segmentation model (SegFormer-class) + ARKit/ARCore planes |
| Designs measured from the floor line, using an assumed visible wall width (Settings) | Designs world-locked in real metres from the AR plane |
| No depth: an object in front of the wall that is the *same* colour as the wall can get painted | LiDAR / ARCore Depth occlusion |
| Room read = on-device light/undertone measurement + curated palettes | Full AI report via the backend + Claude vision |

Next step for full accuracy: swap `track()` for the segmentation model running in the browser. A
TF.js or ONNX-Runtime-Web export of the same model would work, with the region-grow kept as a
temporal stabiliser. The shader and UI stay unchanged.
