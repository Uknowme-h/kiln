"""FastAPI app: health, model routing, the usage counter, and the floor stream."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from kiln import __version__
from kiln.agents.roles import Role
from kiln.config import Settings, load_settings
from kiln.events.demo import DEMO_AGENTS, script_demo_run
from kiln.events.log import EventLog
from kiln.events.schema import NoteAdded, encode_event
from kiln.gateway.budget import BudgetGuard
from kiln.gateway.routing import route_for
from kiln.gateway.usage import UsageLog

logger = logging.getLogger("kiln")

UI_DIR = Path(__file__).resolve().parents[2] / "ui"

logger = logging.getLogger("kiln")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if not settings.model_api_key:
            logger.warning("MODEL_API_KEY is not set. Copy .env.example to .env and add the key.")
        yield

    app = FastAPI(title="Kiln", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.events = EventLog()
    app.state.budget = BudgetGuard(settings.project_token_ceiling)
    app.state.usage = UsageLog()
    app.state.floor_queues: set[asyncio.Queue] = set()
    app.state.demo_task: asyncio.Task | None = None

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "kiln", "version": __version__}

    @app.get("/routing")
    def routing() -> dict[str, dict]:
        return {role.value: route_for(role, settings).public() for role in Role}

    @app.get("/usage")
    def usage() -> dict:
        summary = app.state.usage.summary()
        summary["budget"] = app.state.budget.snapshot()
        return summary

    @app.get("/api/agents")
    def agents() -> list[dict]:
        """The floor roster. Static until runs spawn real per-task workers."""
        return [agent.model_dump() for agent in DEMO_AGENTS]

    @app.get("/events/stream")
    async def stream(request: Request):
        """Server-sent events for the floor view. One queue per browser tab."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        app.state.floor_queues.add(queue)

        async def gen():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {event}\n\n"
                    except TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                app.state.floor_queues.discard(queue)

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def _broadcast(payload: str) -> None:
        for queue in list(app.state.floor_queues):
            queue.put_nowait(payload)

    @app.post("/demo/run")
    async def demo_run(request: Request) -> dict[str, str]:
        """Broadcast the scripted demo run to every connected floor view.
        A new request replaces any run still playing, so runs never interleave.
        Accepts an optional JSON body with an ``idea`` for the Brief overlay."""

        idea = "A to-do app with accounts"
        try:
            body = await request.json()
        except Exception:
            body = None  # no body is fine; the demo has a default idea
        if isinstance(body, dict):
            typed = body.get("idea")
            if isinstance(typed, str) and typed.strip():
                idea = typed.strip()

        async def play() -> None:
            for event in script_demo_run(idea=idea):
                await _broadcast(encode_event(event))
                await asyncio.sleep(1.2)

        if app.state.demo_task and not app.state.demo_task.done():
            app.state.demo_task.cancel()
        app.state.demo_task = asyncio.create_task(play())
        return {"status": "started", "run_id": "demo-1"}

    @app.post("/demo/note")
    async def demo_note(request: Request) -> dict[str, str]:
        """Echo a client note onto the floor. Stands in for POST /runs/{id}/notes
        until real runs exist; the Note Dock is built against this."""
        body = await request.json()
        text = body.get("text", "") if isinstance(body, dict) else ""
        if not isinstance(text, str) or not text.strip():
            return {"status": "ignored"}
        await _broadcast(encode_event(NoteAdded(text=text.strip())))
        return {"status": "noted"}

    if UI_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=UI_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def floor() -> FileResponse:
            return FileResponse(UI_DIR / "index.html")

    return app


def main() -> None:
    uvicorn.run("kiln.main:create_app", host="127.0.0.1", port=8000, factory=True)
