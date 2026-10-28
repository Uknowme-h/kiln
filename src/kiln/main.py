"""FastAPI app: health, model routing, the usage counter, the floor stream,
and the run registry with its companion relay."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from kiln import __version__
from kiln.agents.roles import Role
from kiln.companion.protocol import Hello, decode_companion_message
from kiln.companion.registry import (
    CompanionRegistry,
    CompanionUnavailable,
    PairingError,
)
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
    app.state.pipeline_task: asyncio.Task | None = None
    app.state.runs = CompanionRegistry()

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

    async def _send_floor(event) -> None:
        await _broadcast(encode_event(event))

    async def _start_pipeline(run) -> None:
        """The full loop on a real run, in the background."""
        from kiln.gateway.client import Gateway
        from kiln.graph.orchestrator import run_pipeline

        run.status = "building"
        gateway = Gateway(
            settings,
            BudgetGuard(settings.run_token_ceiling),
            app.state.usage,
        )
        try:
            green = await run_pipeline(
                run.id,
                run.idea,
                run.user_notes,
                gateway,
                app.state.runs,
                _send_floor,
                settings.run_token_ceiling,
            )
            run.status = "green" if green else "failed"
        except Exception:
            logger.exception("pipeline failed for %s", run.id)
            run.status = "failed"

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

    # ── runs and the companion relay ──

    @app.post("/runs")
    async def create_run(request: Request) -> dict:
        """Start a run: returns a one-time pair code. Work begins when the
        local companion claims the code (docs/local-companion.md)."""
        body = await request.json()
        idea = body.get("idea", "") if isinstance(body, dict) else ""
        if not isinstance(idea, str) or not idea.strip():
            return {"error": "a run needs an idea"}
        app.state.runs.expire_stale_codes()
        run = app.state.runs.create_run(idea.strip())
        return {
            "run_id": run.id,
            "pair_code": run.pair_code,
            "status": run.status,
            "connect": f"{settings.companion_command_prefix} connect {run.pair_code} --dir .",
        }

    @app.get("/runs/{run_id}")
    def run_status(run_id: str) -> dict:
        run = app.state.runs.get_run(run_id)
        if run is None:
            return {"error": "unknown run"}
        return {
            "run_id": run.id,
            "status": run.status,
            "workspace": run.workspace,
            "notes": len(run.user_notes),
        }

    @app.post("/runs/{run_id}/notes")
    async def add_note(run_id: str, request: Request) -> dict:
        run = app.state.runs.get_run(run_id)
        if run is None:
            return {"error": "unknown run"}
        body = await request.json()
        text = body.get("text", "") if isinstance(body, dict) else ""
        if not isinstance(text, str) or not text.strip():
            return {"status": "ignored"}
        count = app.state.runs.add_note(run_id, text.strip())
        await _broadcast(encode_event(NoteAdded(text=text.strip())))
        return {"status": "queued", "notes": count}

    @app.post("/runs/{run_id}/probe")
    async def probe_run(run_id: str, request: Request) -> dict:
        """Dev aid: relay one tool call to the attached companion and return
        its answer. Proves the machine is connected and scoped."""
        body = await request.json() if (await request.body()) else {}
        tool = body.get("tool", "write_file")
        tool_input = body.get(
            "input", {"path": "kiln-hello.txt", "content": "the floor was here\n"}
        )
        try:
            output = await app.state.runs.call_tool(run_id, tool, tool_input, timeout=30)
        except (CompanionUnavailable, PairingError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # validation or execution failure on the companion
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "output": output}

    @app.websocket("/companion/ws")
    async def companion_ws(websocket: WebSocket) -> None:
        await websocket.accept()
        run = None
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=30)
            message = decode_companion_message(raw)
            if not isinstance(message, Hello):
                await websocket.close(code=4000, reason="first message must be hello")
                return
            try:
                run = app.state.runs.claim(message.code)
            except PairingError as exc:
                await websocket.close(code=4001, reason=str(exc))
                return
            connection = app.state.runs.attach(run, websocket, message.workspace)
            from kiln.companion.protocol import HelloAck

            await websocket.send_text(HelloAck(run_id=run.id).model_dump_json())
            logger.info("companion attached to %s (%s)", run.id, message.workspace)
            if settings.pipeline_autostart and (
                app.state.pipeline_task is None or app.state.pipeline_task.done()
            ):
                app.state.pipeline_task = asyncio.create_task(_start_pipeline(run))
            while True:
                payload = await websocket.receive_text()
                reply = decode_companion_message(payload)
                if reply.type == "tool_result":
                    connection.resolve(reply)
                # pong needs no handling; its arrival is the liveness signal
        except TimeoutError:
            await websocket.close(code=4002, reason="hello timeout")
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("companion socket failed")
        finally:
            if run is not None:
                app.state.runs.detach(run.id)
                logger.info("companion detached from %s", run.id)

    if UI_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=UI_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def floor() -> FileResponse:
            return FileResponse(UI_DIR / "index.html")

    return app


def main() -> None:
    uvicorn.run("kiln.main:create_app", host="127.0.0.1", port=8000, factory=True)
