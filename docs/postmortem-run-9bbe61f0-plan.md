# Postmortem fixes — run-9bbe61f0

Run: "note taking app with clean UI, excalidraw, accounts, landing page" in
`D:\Projects\Notescalidraw`. Ended not green after T0; no feature was built.

## Failure chain

1. `scaffold_framework(nextjs, ".")` ran `create-next-app@15 .`, which names
   the package after the folder. `Notescalidraw` has capitals → npm refused.
   create-next-app also refuses a non-empty target, and `.kiln/` and
   `docs/discovery.md` exist by then, so a lowercase folder would fail too.
2. The orchestrator treated the failure as advisory. Every later agent was told
   "the companion already scaffolded your root" and built on nothing.
3. Nothing installs the suite's runner: create-next-app has no vitest, node
   `install_deps` only runs `npm install` on an existing `package.json`, and no
   task may own the manifest. A node suite could not run in this run or any.
4. worker-1 died with HTTP 400 after compaction: the kept tail started with a
   `tool` message whose assistant `tool_calls` turn was cut off.
5. `grep` `include: "page.tsx"` matched against `app/page.tsx` → "(no
   matches)" on files the worker had just written; it re-read them instead.
6. The crash was reported as "Rosa Reporter stuck", and the 400 body was
   dropped ("Model API returned HTTP 400").

## Fixes

| # | Fix | Where |
|---|---|---|
| 1 | Argv recipes run in a staging folder under `.kiln/scaffold/` whose child is an npm-valid name derived from the target folder (`Notescalidraw` → `notescalidraw`, `.`'s name for single-root). The CLI sees a fresh, empty, valid target; its output is then moved into the real root. Files that already exist in the root are kept (never overwritten) and reported. Staging is on the workspace volume, so the move is a rename (node_modules is not copied across drives). | `companion/executor.py`, `stacks/scaffold_recipes.py` |
| 2 | A scaffold that fails, or that the user declines, ends the run with a clear `LoopError` naming the framework, root and cause. "Already scaffolded" stays a reuse. | `graph/orchestrator.py` |
| 3 | Node `install_deps` at the suite root guarantees the runner: creates a minimal `package.json` if none exists, adds `vitest` to devDependencies and `"test": "vitest run"` when missing, then `npm install`. The orchestrator installs at the suite root (`.`) for a node suite even when no declared root is `.`. `run_tests` explains a missing runner instead of surfacing npx noise. Dev notes stop telling workers to edit the manifest for vitest. | `stacks/profiles.py`, `companion/executor.py`, `graph/orchestrator.py` |
| 4 | Compaction never keeps a tail that starts with orphaned `tool` messages. The gateway validates tool-call pairing before sending, so a malformed history fails locally with a precise message instead of a remote 400. | `agents/loop.py`, `gateway/client.py` |
| 5 | `grep` `include` matches the workspace-relative path **or** the file name. | `companion/executor.py` |
| 6 | The orchestrator tracks the current stage; a crash is reported on that stage's agent with a "run failed during <stage>" message (not the reporter). Gateway HTTP errors carry the provider's error message (clipped). | `graph/orchestrator.py`, `gateway/client.py` |
| + | Windows shell notes: avoid inline `node -e` / regex one-liners in PowerShell; use grep/read_file for content checks. | `agents/prompts/shell_windows.txt` |

## Status — implemented

All fixes above are in. Notes:

- The staged scaffold is verified against a faked create CLI (it builds
  `<cwd>/<name>` exactly as create-next-app does); it has not been exercised
  against the live npm registry in CI.
- A suite-root install failure is now fatal (the suite cannot run without
  it); installs of secondary roots stay advisory.

## Verification

Unit tests per fix (staged scaffold + name sanitising + no-overwrite move;
scaffold failure stops the run; vitest ensured with/without a manifest and
idempotent; orphan-safe compaction tail + gateway pairing check; grep include by
name; stage-accurate crash report; 400 message surfaced). Full suite + `-m sim`.
