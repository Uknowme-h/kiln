# Kiln floor view

A top-down office where each agent has a desk. Avatars change pose with their
status, envelopes fly when agents hand work off, the CI rack glows red or green,
and a token meter tracks spend. Inspired by office-floor agent viewers; all art
here is placeholder Canvas drawing written for this hackathon.

## How it works

- No build step. `index.html`, `style.css`, `floor.js` are served by FastAPI
  (`/` and `/static/*`).
- The view consumes the typed event contract in `src/kiln/events/schema.py`
  over SSE at `/events/stream`. It never reads run state directly.
- `POST /demo/run` plays a scripted run from `src/kiln/events/demo.py` so the
  floor works before the agent loop calls real models.

## Try it

```powershell
uvicorn kiln.main:create_app --factory --reload
# open http://127.0.0.1:8000/ and press "Run demo"
```

## Later

- Real sprites and a proper tileset replace the placeholder shapes.
- The demo feed is replaced by events emitted from the LangGraph nodes.
