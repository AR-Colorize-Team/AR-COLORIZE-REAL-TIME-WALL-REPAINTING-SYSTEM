# ARColorize Live: technical report (v2.0)

## Pipeline
1. **Capture and de-noise.** The camera frame is uploaded to the GPU. A motion-adaptive temporal filter
   (ping-pong framebuffers) averages still pixels over several frames and passes moving pixels through
   immediately. Camera motion is detected by block-matching the frame against the one 3 frames earlier
   (±3 px).
2. **Tracking (CPU, 224 px; 160/128 px on slower devices).** Linear-light colour, a noise-adaptive
   edge map and a local texture measure. Wall regions grow from the user's tap within colour,
   brightness-band and texture limits, and follow the wall from frame to frame.
3. **Corners and creases.** Soft room corners are found as Hessian valleys of log-luminance and as
   coherent soft steps (structure tensor), kept only where the same plain paint continues on both
   sides, with long lines only. Lines are split and fitted with Hough voting plus iterated total
   least squares on the valley centre, then smoothed over time. Growth never crosses them, and the
   shader cuts paint along each fitted line with a 1-px anti-aliased edge.
4. **People.** The MediaPipe Selfie Segmenter (GPU) gives a person mask every frame. Hysteresis
   includes arms and shoulders connected to the body. People are excluded from tracking and paint.
5. **Surface checks.** A tapped region that is white blow-out and small or broken up (a window or
   light), sky-coloured, strongly patterned or tiny is refused with a message.
6. **Rendering (WebGL).** Pass 0 computes per-pixel wall coverage: joint bilateral upsampling of the
   tracker masks guided by colour, a full-resolution colour/brightness test against nearby confirmed
   wall (tolerant of camera sharpening halos), corner-line clipping, person mask and coverage memory
   while still. Pass 1 relights and composites: new = paint albedo × measured light (shading from
   luminance, or from the darkest channel on over-exposed walls), local light-colour drift,
   inter-reflection in corners, an exposure cap, and the finish model (matte scatter; gloss depth,
   contrast, window glare and highlights in the light's colour). Then the touch-up brush, patterns
   and the compare split are applied.

## Wall today (reference albedo)
The camera measures wall albedo × light. With the wall's current colour known, the light is exactly
observed ÷ albedo, so new paint renders under the true light. *Match* picks the closest reference
colour from the camera, and a consistency check limits the effect of a mismatched choice.

## Results
Synthetic room with ground truth (same-paint ceiling and side wall with soft corners, beige floor,
blown-out window, pale curtain, door, switch, furniture, sensor noise, camera tone curve), moving
camera, scored on the final shader output:

| Surface (tap on the main wall) | Share painted |
|---|---|
| Ceiling | 0% |
| Floor | 0% |
| Side wall | 0% |
| Curtain | 0.1–1.1% |
| Objects (frame, switch, furniture) | 1.0–1.5% |

- Window and light taps refused: 3/3.
- Colour fidelity (mean CIEDE2000, 12 paint × room combinations, against a true repaint render): **1.93** with *Wall today*, 3.27 auto.
- Steady picture (still camera, fresh sensor noise each frame): 0.11% of pixels change per frame.
- Laptop (integrated GPU, Edge): 30 fps, tracking 7–11 ms, segmentation 7–10 ms.

Reproduce with the scripts in `bench/` (see `bench/README.md`).
