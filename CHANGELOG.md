# Changelog

## v3.0.0, November 2026: ARWall
Full-stack release in `arwall/`. New live web app (`arwall/live/`, now served on arcolorize.vercel.app)
with a new app icon; native Flutter AR app with ARKit/Metal (iOS) and ARCore/GLES3 (Android) engines
(`arwall/app/`); AI room analysis and reference paint model (`arwall/engine/`); FastAPI backend
(`arwall/backend/`); PostgreSQL schema and docker-compose (`arwall/db/`). The v2.0 app stays in `web/`.

## v2.0.0, November 2026: ARColorize Live
First working release. A real-time AR wall painter in the browser (`web/`):
tap-to-paint tracking with corner, ceiling and floor detection; AI person occlusion; window
rejection; physically based relighting with *Wall today*; finishes; designs and styles;
temporal de-noising (no flicker); touch-up brush; installable PWA. Includes an evaluation bench (`bench/`).
Details: `docs/TECHNICAL_REPORT.md`.

## v1.0, November 2025 to April 2026: Proposal
Project aim, problem statement, advantages, proposed features, team and planned stack (README).
