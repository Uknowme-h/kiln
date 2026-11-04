# Kiln floor view

A top-down office where each agent has a desk. Avatars change pose with their
status, envelopes fly when agents hand work off, the CI rack glows red or green,
and a token meter tracks spend. Inspired by office-floor agent viewers; all art
here is placeholder Canvas drawing written for this hackathon.

The client console wraps the floor: the **Brief** (idle overlay where you type
the idea), the **pairing panel** (the pair code and connect command while the
run waits for your machine), the **Note Dock** (a composer for mid-run notes
to the team), and the **Handoff** (the results card when the run finishes).
Your notes land on the cork board as lemon stickies.

**Click any desk** to open that agent's drawer: the recorded model calls
(prompts in, completions out, token counts, finish reason) and tool calls
(args, results, errors) for the run, as a chat-like thread. It live-updates
while the run is on and stays readable from the Handoff screen.

**Approvals** show on the floor as well as in the companion terminal; first
answer wins. **Allow all** in the dialog, or the header **auto-approve** switch,
auto-approves every command for the rest of the run, like the companion's
`--auto-approve` flag. The plan review and the agents' questions always ask.
Auto-approved commands show up as log lines, not dialogs
(`POST /runs/{id}/auto-approve`, or `scope: "all"` on `POST /runs/{id}/approvals`).

## The floor plan

Three rooms along the top (meeting, plan office, QA lab), a corridor, then the
build floor (six shared desks, 3×2, with the sprint board) and the support
desks over the break nook. Desk columns line up across rooms. Every door sits
on an aisle, and every desk has the same margins and gutters (40px between
desks, a 168px label lane each).

- Agents move on a network of lanes (corridor, aisles, behind-the-desk lanes)
  and take the shortest path, so they go around furniture and through doors.
- Workers share the six build desks. A new worker walks in at the entrance
  and takes a free desk, or the desk of whoever finished first, who then
  walks out. If all six are busy, extra workers sit at the meeting table.
  A worker who has left can no longer be clicked for their transcript.
- Between jobs, idle agents get coffee, chat at the water cooler, sit on the
  couch, read the sprint board, look around, stretch, or doze off at the
  desk. Work calls them straight back.

## What the floor tells you at a glance

- **Monitors**: code scrolls while an agent works, a cursor blinks while it
  thinks, and the screen flashes red with "!" when it's stuck.
- **"?" bubble**: that agent is waiting on your approval or answers.
- **Dotted lines**: who just messaged whom (fades after a few seconds).
- **Sprint board**: cards move across to do → doing → done as work
  progresses. Your own notes are lemon cards with a coral corner.
- **End of a run**: green brings confetti and a sweep of light along the CI
  rack. Red sets the rack blinking, and the fixer walks over to it.
- **Time of day**: the lighting follows the wall clock. Evenings turn warm,
  nights go blue, and busy desks light their lamps. Clouds drift past the
  windows by day and stars come out at night. Add `?hour=21` to the URL to
  preview a time.
- **Habits**: the analyst drafts the spec on the whiteboard (it fills in as
  they write), the planner paces by the table while thinking, the test author
  checks the CI rack, and the reviewer and reporter visit the printer.
- **Drawer focus**: the agent open in the drawer gets a ring under their feet.
- **Crisp pixels**: the canvas snaps to whole screen pixels per art pixel
  when that costs at most 12% of the size.
- **Reduced motion**: with the OS setting on, idle wandering, bobbing,
  confetti, drifting clouds and the fan all stop. Work-related walking stays,
  because it carries information.

## How it works

- No build step. `index.html`, `style.css`, and the JS files are served by
  FastAPI (`/` and `/static/*`).
- Rendering: the static scenery is painted once to an offscreen canvas and
  blitted each frame. Only the clock, CI rack, sticky notes, people,
  envelopes and particles are redrawn. The simulation steps at a fixed 60 Hz,
  so the speed is the same on any refresh rate. Behind an overlay the floor
  dims inside the canvas and drops to ~20 fps.
- The view consumes the typed event contract in `src/kiln/events/schema.py`
  over SSE at `/events/stream`. It never reads run state directly.
- The Brief calls `POST /runs`, then polls `GET /runs/{id}` until your local
  companion claims the pair code; the Note Dock then posts to
  `POST /runs/{id}/notes`. The pipeline starts driving the floor when the
  full loop lands.
- The drawer fetches `GET /runs/{id}/transcript?agent=...`, fed by the
  per-run `TranscriptLog` (`src/kiln/agents/transcript.py`). Recording happens
  at the two choke points that see everything — `Gateway.complete` for model
  I/O and the agent loop / orchestrator for tool I/O. The SSE floor only
  carries lightweight `transcript_tick` events; the heavy entries stay on the
  server until clicked. Transcripts are in-memory and die with the run, like
  the rest of the registry.
- The header demo button plays the scripted rehearsal run from
  `src/kiln/events/demo.py` (`POST /demo/run`), and `POST /demo/note` echoes
  notes onto the floor during rehearsals. The demo has no real transcript.

## Try it

```powershell
uvicorn kiln.main:create_app --factory --reload
# open http://127.0.0.1:8000/, type an idea (or pick a chip), Brief the team,
# then run the shown connect command on your machine
```

The connect command the UI shows comes from the server's
`COMPANION_COMMAND_PREFIX` setting. Until the package is on PyPI the default
is `uvx --from git+https://github.com/Uknowme-h/kiln kiln-companion`; from a
repo checkout, `.venv\Scripts\kiln-companion connect CODE --dir .` works with
no network (run `pip install -e .` once after pulling, so the script exists).

## Later

- Real sprites and a proper tileset replace the placeholder shapes.
- The demo feed is replaced by events emitted from the LangGraph nodes when
  the full loop drives real runs.
