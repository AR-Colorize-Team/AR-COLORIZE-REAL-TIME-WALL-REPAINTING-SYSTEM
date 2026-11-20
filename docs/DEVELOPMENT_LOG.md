# AR Wall Live: v2 changes

`live/index.html` v2 fixes the four problems seen with v1: paint spilling onto the ceiling, floor and the next wall; windows getting painted when tapped; ragged edges; and colours that didn't look like the real paint. Everything still runs in one HTML file with no dependencies.

## How to install

Copy `index.html` and `sw.js` from this folder over the ones in `arwall/live/`. Keep a backup of the old `index.html` first. The service-worker cache name moves to `arwall-live-v2`, so installed copies update themselves.

## What changed

### 1. Paint stops at room corners (ceiling, floor, next wall)
Corners between surfaces painted the same colour are soft shaded valleys, not edges, so v1's region growing ran straight through them. v2 adds a **corner (crease) detector**:

- **Valleys and ridges.** It uses the Hessian of log-luminance and judges each response against the camera's own noise, measured each frame with a robust MAD of the Laplacian.
- **Soft steps.** It uses the gradient's coherence over a 5×5 window (structure tensor).
- **Same paint on both sides.** A crease only counts where the same plain paint continues on both sides of it, sampled 3–4 px away along the normal. So the detector never fires beside a door or picture frame, and no halo is left there.
- **Long lines only.** Short outlines, such as a switch or a nail, are dropped.
- **Gap closing.** Lines are extended along their direction, three-way junctions are handled, and lines are thickened by 1 px to close gaps.
- **Leak cleanup.** Any part of the region that sits across a corner from the main wall is dropped, so a one-frame leak heals on the next frame.
- **Curtains and blinds.** Several parallel fold lines a few pixels apart are recognised as fabric and blocked.
- **Texture gate.** The average edge energy over 5×5 keeps paint off patterned surfaces such as fabric, wallpaper and tiles.
- **Brightness band** (paper, eq. 5) around the wall's own level.

### 2. Taps on windows, lights and non-walls are refused
After the first growth, the tapped region is checked: share of clipped pixels, brightness against the frame median, sky-blue chromaticity, texture and size. The user gets a clear message such as "That looks like a window or a light. Tap the wall itself." Re-acquiring a lost wall now also requires matching brightness, so it can no longer jump to a different surface.

### 3. Crisp edges
- **Joint bilateral upsampling** of the tracker mask in the shader (Kopf et al. 2007), guided by a low-res colour texture.
- **Full-resolution wall-colour test.** Each edge candidate must match the colour of the confirmed wall right next to it, so a cream frame or cushion on a cream wall stays unpainted.
- **Straight corner edges.** Each long corner is fitted with a straight line: Hough, then iterated total least squares on the thinned valley centre, then duplicates merged and the line smoothed over time. The shader cuts the paint along that line with a 1-pixel anti-aliased edge, because no colour test can place an edge between two surfaces painted the same colour.
- Tracking runs at 224 px, stepping down to 160 and then 128 px on slow phones. Painted walls carry over when the resolution changes.
- **Asymmetric temporal smoothing.** A person walking in clears from the mask at once, and newly visible wall fades in.

### 4. More realistic colour
- **"Wall today" picker** in the Colours tab: Auto, eight common wall colours, or any colour. With the current wall colour known, the light's colour and the wall's reflectance are exact, so the preview is relit physically: `P · I / A`. This was future work in the paper.
- **Local light colour.** The paint picks up lamp-warm and window-cool areas from how the wall's colour drifts across it.
- **Inter-reflection.** Saturated paints get slightly richer in corners and shadows, as real paint does.
- **Sheen** on glossier finishes now takes the light's colour, not the paint's.
- The demo room now has a ceiling and a side wall, so the corner handling is visible in the demo.

## Benchmark: same synthetic room, before and after

A hard room with ground truth: a ceiling and a side wall in the **same paint** as the main wall with soft corners, beige floor tiles, a blown-out window, a pale curtain, a door, a switch and furniture. Two variants (warm off-white with a 3300 K lamp, lavender in daylight), a moving handheld camera, sensor noise, a tone curve and lens blur. Scores come from the **final shader output at full resolution**.

Spill = share of each surface that got painted after tapping the main wall:

| | Ceiling | Floor | Side wall | Curtain | Objects | Total spill |
|---|---|---|---|---|---|---|
| v1 | 100% | 1–97% | 100% | 0–40% | 3–5% | 38–51% |
| **v2 (224 px)** | **0%** | **0%** | **0%** | **0.1–0.9%** | **0.3–1.0%** | **0.6–1.3%** |
| v2 (160 px fallback) | 0% | 0% | 0% | 0.1–0.9% | 0.3–0.7% | 0.7–1.4% |

Total spill is for taps on the open main wall. Tapping the small patch of wall under the window, which the curtain cuts off, gives 7.8% spill in v2 against 51% in v1, because that region is tiny. Window taps (sky, trees, lavender room): v1 painted 20–29% of the window in two of three cases. **v2 refused all three** with a message.

Colour fidelity, mean CIEDE2000 against a true repaint render, 12 paint and room combinations:

| | Mean ΔE00 |
|---|---|
| v1 (auto) | 3.22 |
| v2 (auto) | 3.27 |
| **v2 with "Wall today" set** | **1.93** |

Tracking cost on a 2-core 2.1 GHz Xeon VM (no GPU): about 23 ms at 224 px and about 13–15 ms at 160 px. The app steps down automatically when tracking exceeds 14 ms. Phone CPUs run JavaScript considerably faster than this VM.

## Known limits
- The part of a wall cut off by a full-height curtain or bookshelf is a separate region. Tap it as a second wall; the app supports three.
- In auto mode the app assumes a reflectance of 0.65, so an off-white wall previews slightly too light (+3–4 L*). Setting "Wall today" fixes this.
- All numbers are synthetic. Next steps are a real-phone test and before/after photos with a colour chart.

## Reproduce
`bench/`: `make_scene.py` (room, labels and ground-truth repaints), `harness.py` (segmentation spill and IoU, headless Chrome), `color_bench.py` (CIEDE2000), `summarize.py`. Requires Python, OpenCV, NumPy and Playwright with Chrome.

The harness needs a copy of v1 with the same debug hook, which is in v2 as `window.__dbg = 2`.

## v2.1: tuned on a real laptop webcam (light-blue room, bright window light)
- **Brighter walls are accepted.** A light, over-exposed wall clips one colour channel (here blue) first. That no longer counts as a "window"; only white blow-out that is also small or broken up does. The "textured" check is now relative to the camera's own noise, so JPEG chroma noise isn't mistaken for a pattern. Rejection overall only triggers in clear cases.
- **Colour-clipped highlights still count as wall** on bright walls, so the paint no longer has holes where the light is strongest.
- **Shading on over-exposed walls** comes from the darkest colour channel, which still sees the light, so the paint isn't flat. The paint level is capped at a normal exposure, so colours aren't washed out.
- **No more fringes.** Webcams add a bright sharpening ring around dark objects (a fan, hair, a frame). That ring is now treated as wall. A "corner" also needs similar brightness on both sides, so small dark objects no longer get an unpainted ring.
- **Wall today** now has 16 colours, including light and sky blue, aqua, light green, lemon, pink and grey. A new **Match** button picks the closest one from the camera. If the chosen colour clearly doesn't match what the camera sees (for example "White" on a blue wall), the app warns you and stops trusting it for the light's colour, so the paint isn't tinted.
- The synthetic benchmark is unchanged: 0% on the ceiling, floor and side wall, with windows refused. Object spill is 1.5% (was 0.8%).

## v3: people, finishes, touch-up, smoother edges (tested on a real webcam)
- **AI person occlusion.** The MediaPipe Selfie Segmenter (about 250 kB, GPU, about 7 ms per frame on the test laptop) marks people every frame. It loads from the jsDelivr CDN on camera start. Without internet the app works as before, and engineer mode shows the status. Hysteresis treats weakly detected arms and shoulders as person when they connect to the confident body. The tracker never grows paint into people, and the shader cuts paint at the person's edge with image-guided upsampling. This fixes paint on a wall-coloured shirt and halos around hair. The toggle is in Settings, "Keep people unpainted".
- **Finishes now visibly differ.** Matte is slightly chalky and soft. Glossier finishes get deeper colour, more shading contrast, a glow toward bright surroundings (a window or lamp) and highlights in the light's colour. Each finish has a one-line description.
- **Touch-up brush** (pencil button). It pauses the frame and offers Paint and Erase with a size slider, Clear and Done. Strokes are applied in the shader on top of the automatic mask and are included in saved photos.
- **Smoother, steadier outline.** The low-resolution mask is smoothed before upsampling, which removes the staircase effect. Temporal smoothing no longer makes the edge wobble on noisy webcams, while people still clear at once.
- **Corners.** The wall side of a corner line is decided from a wider area, and spill beyond the line is cleared up to 10 px.
- The start screen lists the capabilities. The synthetic benchmark is unchanged (0% on the ceiling, floor and side wall; windows refused).

## v4: no flicker when the camera is still
The flicker came from sensor noise. Every frame is slightly different even with a still camera, and each frame the paint edge was decided afresh. Three layers fix it, and all of them switch off as soon as the camera moves, so nothing lags or smears:
- **Motion detection.** The frame is block-matched against the one from 3 frames ago at ±3 px shifts. If any shift fits better than none, the camera is moving. Brightness change alone can't distinguish a slow pan across a plain wall from noise.
- **GPU temporal denoiser** for the camera image, using ping-pong framebuffers. While still, pixels are averaged over several frames; pixels that change (a hand, a person) update immediately. Paint and edge tests read the denoised image.
- **CPU temporal filter** of the tracker image, plus steadier corner lines while still.
- **Coverage memory.** A two-pass render: pass 0 decides each pixel's paint coverage and blends it with the previous frame's while still; pass 1 paints. A person stepping in clears at once.

Measured on a still webcam frame with fresh sensor noise every frame:

| | Pixels switching per frame | Flickering area |
|---|---|---|
| v3 | 1.3% | 5.4% |
| v4 | **0.11%** (12× less) | **1.8%** |

The moving-camera benchmark is identical to v3. Toggle: Settings → "Steady picture".
