# Postmortem — run-aa748f20 (calidraw: notes + Excalidraw, FastAPI + Vite)

Read-only analysis of `.kiln/runs/run-aa748f20/` (events, transcript, usage).
The live session was not touched.

## Timeline (minutes from start)

| t | what happened |
|---|---|
| 8:06 | both scaffolds start (Dev A backend, Dev B frontend) |
| 10:22 | Dev A's attempt is killed as "stuck repeating the same call"; the task restarts from scratch |
| 16:29 | Dev B finishes its scaffold — and then waits |
| 20:41 | Dev A finishes its scaffold |
| 20:41–25:23 | Kiln installs pip deps (2:20), then npm deps (2:22), one after the other |
| 25:24–26:11 | the suite "cannot run", a reinstall at the workspace root, the suite "cannot run" again |
| 26:12 | the first feature tasks start; Dev B was idle ~10 min, Dev A ~5.5 min |
| 40:23–48:23 | the companion disconnects; every tool call waits 90 s, then fails; Dev A escalates |

Tokens: Dev A ~1.9M prompt tokens over 88 calls (129 `read_file`, 15 `skill`);
Dev B ~1.4M over 67 calls.

## Root causes and fixes

1. **Stage barrier (the idle developers).** Feature tasks waited for *every*
   scaffold, then for installs of every root in sequence, then a suite run.
   → One scheduling pass (`_run_tasks(prepare=True)`): a layer-0 task installs
   its own root and is smoke-checked inside its developer's job, then counts
   as done, so the next task on that root starts straight away. Tasks also
   wait until their root is installed. When all of layer 0 has settled, the
   remaining roots (the node test runner at the workspace root) install and
   the suite runs once to give later tasks a test state. Smoke checks and
   repairs no longer block the scheduler. A failed install is recorded, never
   fatal, and never strands tasks.

2. **The suite could never run: the test report was offloaded.** The 28k-char
   JSON report went over the companion's offload threshold and came back as an
   "offloaded" stub; the orchestrator's JSON parse failed, so every run counted
   as "could not run" — including the final green check.
   → `run_tests`, `served_ui_audit`, `runtime_smoke`, `ensure_browser_runtime`
   and `browser_verify` are never offloaded; a stub is reported as "restart the
   companion" instead of a JSON error.

3. **Two Python environments.** `install_deps(root=backend)` built
   `backend/.venv`; the suite and the developers' notes use the workspace-root
   `.venv`, which had only pytest ("No module named 'fastapi'").
   → One environment at the workspace root; each Python root's
   `requirements.txt` installs into it.

4. **Re-reads after compaction, and the false "stuck" kill.** Compaction
   triggered at 100k chars of the *whole* conversation, but a 20k-char brief
   plus a 10k system prompt left room for only a few reads. After each
   compaction the developer re-read the same files, and the repeat guard (whose
   counts survived compaction) refused them, then killed the attempt.
   → The threshold measures only the history after the brief. Compaction resets
   the repeat guard. A repeated read whose result is still in the conversation
   gets a short "Unchanged" pointer (an own write or a pruned result makes the
   next read real again). The post-compaction message lists the files already
   read and the skills loaded. Refused repeats are written to the transcript.
   A second compaction re-wraps the original brief, not the first wrapper.

5. **Required skills loaded again and again.** Each load cost a full turn (the
   whole brief re-sent) and was lost at every compaction: `fastapi` and
   `eng_quality` were loaded seven times each.
   → A task's required skills are embedded in its system prompt (which
   survives compaction); a `skill` call for one gets a one-line pointer.
   Optional skills stay on demand.

6. **Companion outage fed the team errors.** For eight minutes each call waited
   90 s and failed; the developers spent model calls, a compaction and an
   escalation on it, and the lead's own context reads stalled.
   → A call that never reached a companion is held until it reattaches (the
   agent is simply paused; `companion_outage_wait_seconds`, default 900, one
   deadline per outage). The floor says "paused" and "back". A call cut off
   mid-flight is still not repeated.

7. **The whole suite read for a scaffold.** The brief listed every test file
   as "read if a seam touches it"; a scaffold touches every seam.
   → The brief separates the task's own test files (read them whole) from the
   rest (grep them for the route, field or import you provide; read only the
   matching lines).

8. **A spec oracle named a file the suite never wrote** (`tests/test_health.py`
   vs `tests/test_embed_health.py`). → After the suite is written,
   `test_author.align_oracles` points each such oracle at the suite file that
   covers the criterion (left as is, with a run note, when none or several
   do).

9. **Teammate edits vs the "Unchanged" pointer.** Any message from the
   teammate (a finished task, a contract change, files a shell touched) clears
   the cache, so the next read of any file is real.

## Not changed

- A task that starts before every scaffold has settled gets no test state
  (the first suite run happens once layer 0 has settled). That is the price
  of not waiting; later tasks get it.
- The contract is most of the brief (~15k of 20k chars). It could be split by
  side (API routes for the backend developer, the client view for the
  frontend developer).
- `graph/pipeline.py` (unused LangGraph code) and its test keep their two lint
  findings until you decide whether to keep it.

## Verification

`tests/test_run_aa748f20_fixes.py` and `tests/test_worker.py` cover each fix.
Full suite: 610 passed; `pytest -m sim`: 13 passed. Restart the server **and** the companion to pick
up the changes (the offload and Python environment fixes live in the
companion).
