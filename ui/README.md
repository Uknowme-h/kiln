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

## How it works

- No build step. `index.html`, `style.css`, and the JS files are served by
  FastAPI (`/` and `/static/*`).
- The view consumes the typed event contract in `src/kiln/events/schema.py`
  over SSE at `/events/stream`. It never reads run state directly.
- The Brief calls `POST /runs`, then polls `GET /runs/{id}` until your local
  companion claims the pair code; the Note Dock then posts to
  `POST /runs/{id}/notes`. The pipeline starts driving the floor when the
  full loop lands.
- The header demo button plays the scripted rehearsal run from
  `src/kiln/events/demo.py` (`POST /demo/run`), and `POST /demo/note` echoes
  notes onto the floor during rehearsals.

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
