# Yes-app (note app with UI, Excalidraw, accounts, landing)

**Baseline (pre items 9–13):** 2026-11-02, ~427k tokens, 0 fix rounds, vitest
green but generic (localStorage auth, CDN Excalidraw, system UI). Review in
`docs/architecture-research.md` ("Product quality gap").

**Re-run (post items 9–13):** 2026-11-02, workspace `yes-app-v3`, run
`run-a97e29e5`, **green** in 1720s / 1.20M tokens; real sessions + SQLite +
design.md; Excalidraw island written but not bundled into the served page.
See `evals/results/yes-app-v3-2026-11-02.md`.

Build a note taking app with clean UI, excalidraw integrated, user accounts,
login/sign up and landing page aswell
