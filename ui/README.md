# Kiln floor view

A top-down office where each agent has a desk. Avatars change pose with their
status, envelopes fly when agents hand work off, the CI rack glows red or green,
and a token meter tracks spend. Inspired by office-floor agent viewers; all art
here is placeholder Canvas drawing written for this hackathon.

The client console wraps the floor: the **Brief** (idle overlay where you type
the idea), the **Note Dock** (a composer for mid-run notes to the team), and
the **Handoff** (the results card when the run finishes). Your notes land on
the cork board as lemon stickies.

## How it works

- No build step. `index.html`, `style.css`, and the JS files are served by
  FastAPI (`/` and `/static/*`).
- The view consumes the typed event contract in `src/kiln/events/schema.py`
  over SSE at `/events/stream`. It never reads run state directly.
- `POST /demo/run` plays a scripted run from `src/kiln/events/demo.py` so the
  floor works before the agent loop calls real models; it accepts an optional
  `{"idea": "..."}` body from the Brief.
- `POST /demo/note` echoes a `note_added` event onto the floor, standing in
  for `POST /runs/{id}/notes` until real runs exist.

## Try it

```powershell
uvicorn kiln.main:create_app --factory --reload
# open http://127.0.0.1:8000/, type an idea (or pick a chip), and Brief the team
```

## Later

- Real sprites and a proper tileset replace the placeholder shapes.
- The demo feed is replaced by events emitted from the LangGraph nodes, and the
  Brief/Note Dock switch from the demo endpoints to `POST /runs` and
  `POST /runs/{id}/notes` (handlers are marked in `app.js`).
