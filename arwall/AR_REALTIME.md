# Realtime AR painting, wall designs & premium AI analysis

```
app/                                   Flutter app (UI) + native AR engines
├── lib/
│   ├── main.dart                      Home, friendly camera pre-prompt, AR support check
│   ├── core/theme.dart                Design tokens (same as the design canvas), GlassPanel
│   ├── core/api.dart                  Backend client: AI job + polling, Stripe upgrade
│   └── features/
│       ├── ar/ar_controller.dart      Channel bridge, coaching states, undo, litres estimate
│       ├── ar/ar_camera_view.dart     Native view embed (UiKitView / Hybrid Composition)
│       ├── ar/ar_paint_screen.dart    Live screen: tap wall · colours · designs · styles · AI · compare
│       ├── designs/designs.dart       8 wall patterns, 4 finishes, 7 room style presets
│       └── ai/ai_report_screen.dart   Premium report, "Try on my wall", quota paywall
├── ios/Runner/ARWall/                 ARKit + Metal  (LiDAR depth, mesh classes, person occlusion)
└── android/app/src/main/              ARCore + GLES3 (vertical planes, Depth API, TFLite GPU)
engine/
├── reference/paint_math.py            Golden model the shaders are ported from (+17 tests, previews)
└── ai/room_analysis.py                Measured light/undertones + Claude vision report (+5 tests)
```

## How the live paint works (both platforms, one shader design)

For every camera frame, in one GPU pass:

1. **Which pixels are wall?** A pixel is painted only if all of these hold:
   * the on-device segmenter says "wall" (it suppresses doors, windows, switches, art, furniture and people);
   * the camera ray through it hits the tapped AR wall plane inside its extent;
   * on LiDAR iPhones or ARCore Depth devices, the measured depth agrees with that plane, so a sofa in front of the wall keeps its pixels.
2. **Where on the wall is it?** The ray–plane hit gives coordinates in metres on the wall. Stripes, arches and the other patterns are drawn in those coordinates, so they stay fixed to the wall while you walk around.
3. **Recolour without losing the light.** New paint colour × the observed shading ratio × the scene light tint. Shadows, lamp glow and plaster texture survive, and dark paint reads genuinely dark. The finish (matte → gloss) controls how much of the original highlights is kept.
4. **Compare.** A before/after split runs inside the same shader at no extra cost.

## Things to know before shipping

* **The segmentation model is not included yet.** The engines expect `WallSegmenter.mlmodelc` (iOS) and `wall_segmenter_fp16.tflite` (Android): a SegFormer-B0-class model with a single P(wall) output. Training and export are Part 3. Until then, painting falls back to "nothing painted" because the mask is empty. For a quick demo, return a mask of all 1s and you get plane-only painting.
* **The native code has not been compiled.** This workspace has no Xcode, Android SDK or Flutter. Expect a short round of compile fixes on first build, mainly API-version details. The shader math itself is verified by the Python golden model and its tests.
* **Screen orientation is portrait only.** The renderer assumes portrait; the AR screen locks it.
* **Plane extent rotation.** On iOS 16+ the `ARPlaneAnchor.planeExtent.rotationOnYAxis` value is ignored. The +25 cm extent margin plus the segmentation mask absorb it, but apply the rotation if you see edges clipping on angled planes.
* **AI analysis uses `claude-sonnet-5-5`** (vision + forced tool output). If the call fails, a curated, light-aware recommender still returns three palettes, so the user never sees an error. The output matches the Part 2 worker contract (`routes/ai.py`).
