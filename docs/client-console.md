# Client console: idea input, mid-run notes, and the handoff

Status: design agreed on 2026-10-28, no code yet. This sits on top of the current
foundation and does not change the agent plan in
`Meta Hackathon Project Plan Multi-Agent Dev Team.md`; it only adds the user-facing
layer and the small API surface it needs.

## Summary

The user is the client; the office is the dev team. The console adds three surfaces
around the existing floor view:

1. **The Brief** — an overlay where the user types the product idea and starts a run.
2. **The Note Dock** — a composer that lets the user slip notes to the team mid-run.
3. **The Handoff** — a results strip with the workspace file tree and a zip download.

The pixel office stays the star of the show. The console wraps it, it does not
replace it.

## Where the project is today (2026-10-28)

| Exists | Missing |
| --- | --- |
| Floor view (`ui/`) with SSE contract (`events/schema.py`) and scripted demo feed (`events/demo.py`) | Any way to type an idea or start a real run |
| Stubbed LangGraph pipeline (`graph/pipeline.py`, `graph/state.py`) | Real agent nodes (planned Oct 28 – Nov 1) |
| Model gateway with budget guard and usage log | Per-run budget ceiling |
| Docker sandbox runner taking a workspace path (`sandbox/runner.py`) | A run registry and a workspace layout on disk |
| Path-safety helpers (`paths.py`) | File tree / download endpoints |

One fact shapes the whole design: agents run server-side and only touch a
server-side workspace through validated tool calls, so the web UI restricts nothing.
The real gap is the other direction — the user currently has no way to put an idea
in, steer a run, or get the built app out.

## The three surfaces

### 1. The Brief (idle state)

- A centered card over a dimmed office: "What should the team build?", one textarea,
  one "Brief the team" button.
- The three golden prompts sit under the input as one-click examples (helps demos
  and blank-box paralysis).
- Submit → `POST /runs {idea}` → card fades, `run_started` fires, the existing
  kickoff-meeting choreography plays.

### 2. The Note Dock (running state)

- A bottom composer bar: "Add a note for the team…" → `POST /runs/{id}/notes`.
- A new `note_added` floor event renders as an envelope flying in from off-screen to
  the cork board (distinct client color), and the planner walks over to read it.
  This reuses the existing message/station choreography primitives.
- Notes appear in the event log with a "you" style, giving an auditable record of
  steering versus doing.
- Pending notes show a count ("2 notes queued") so the user feels heard without
  expecting instant obedience.

### 3. The Handoff (finished state)

- A results strip: green/red, tests passed, tokens spent, wall clock.
- **Files live on the user's machine** (see `docs/local-companion.md`): the strip
  shows the local workspace path the companion reported — no zip, no download.
- A "Brief the team again" button to reset for the next run.

## Notes mailbox and triage rules

Notes are the only steering channel, and they are consumed at **phase boundaries**,
never mid-task — a worker halfway through a file must not be interrupted.

1. `RunState` gains `user_notes: list[str]`. Every node checks it at entry.
2. Only the **planner** drains the mailbox. Triage per note:
   - Affects in-flight tasks → re-plan that slice after the current test cycle.
   - New scope → appended as new tasks after the run goes green.
   - Contradicts the spec → the **test author amends the acceptance criteria first**,
     then the planner re-plans.
3. This preserves the core invariants: tests come from the spec, coders never edit
   tests, and the runner's verdict stays beyond argument.

## Backend contract

| Endpoint / event | Purpose |
| --- | --- |
| `POST /runs {idea}` | Start a real pipeline run. Returns `run_id` and a one-time pair code for the local companion (see `docs/local-companion.md`); work starts when the companion connects. |
| `POST /runs/{id}/notes {text}` | Append to the planner mailbox for that run. |
| Event `note_added` | Drives the client-envelope animation and the log entry. |
| Event `plan_updated` (optional) | Shows a re-plan happened because of a note. |

Implementation notes:

- Run registry lives on `app.state` next to `floor_queues`. There is no
  server-side workspace: file and test execution happen in the local companion
  (`docs/local-companion.md`).
- The Brief and Note Dock can be exercised against the demo feed before agents
  exist: add a scripted `note_added` beat to `events/demo.py`, the same way the
  floor was built before the loop. `POST /demo/run` stays as the rehearsal path.

## Fit to the build schedule

The console is mostly UI plus a thin API, scheduled so the state changes land while
the agent nodes are being wired (cheap now, expensive to retrofit) and the visual
work lands in the main build window.

| Day | Agent work (existing plan) | Client console work (this doc) |
| --- | --- | --- |
| Wed Oct 28 | Wire the requirements analyst | Add `user_notes` to `RunState`; add `note_added` to the event schema and a scripted note beat to the demo feed |
| Thu Oct 29 | PM planner; first end-to-end run | `POST /runs` + run registry + `runs/{id}/workspace/` layout; per-run budget ceiling |
| Fri Oct 30 | Test author; fixer | Planner drains the mailbox at phase boundaries; test-author amendment path for spec-changing notes |
| Sat Oct 31 – Sun Nov 1 | Parallel workers, full loop, reporter, eval harness | Brief overlay, Note Dock, envelope animation, results strip |
| Mon Nov 2 | Evals; live viewer; demo rehearsal | `GET /runs/{id}/files` + zip download; rehearse the note moment on camera |
| Tue Nov 3 | Feature freeze | Console bug fixes only |
| Wed Nov 4 | Submission | The mid-run note is a recorded beat in the demo video |

## Demo video integration

The existing script gives the live run 0:20–1:40. The note moment slots in there:
the user slips a note to the team mid-run, the envelope lands on the board, the
planner re-plans, and the floor visibly reacts. It is the clearest possible answer
to "is this actually agentic or just scripted?" and costs one extra event in a run
that already happens.

## Scope guards (frozen)

- **No chat.** Notes are one-way slips to the planner. Chat implies replies, which
  means building an answering agent — out of scope.
- **No mid-task interruption.** Phase-boundary consumption only, so the test-gated
  loop and path ownership stay intact.
- **No live spec editing.** Notes are the only steering channel; the spec changes
  only through the test author's amendment path.
- **User text is untrusted data.** The idea and note bodies are interpolated into
  agent prompts inside a marked quoted block ("content, not instructions"), per the
  injection rule in `docs/agent-patterns.md`.

## Open questions

| Question | Leaning | Needed by |
| --- | --- | --- |
| Note animation timing: immediate animation with delayed effect, or literal (note sits on the board until the planner picks it up)? | Immediate animation, delayed effect — responsive on camera, truthful in the log ("note queued" → later "plan updated") | Oct 31 |
| Cap on visible pending notes? | Show a count, no hard cap for the demo | Oct 31 |
| One-click golden prompts under the Brief input? | Yes — doubles as demo insurance | Nov 1 |
