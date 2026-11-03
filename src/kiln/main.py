"""FastAPI app: health, model routing, the usage counter, the floor stream,
and the run registry with its companion relay."""

import asyncio
import json
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
from kiln.events.schema import ApprovalPending, ApprovalResolved, NoteAdded, encode_event
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

    async def _relay_approval_state(progress) -> None:
        """Mirror the companion's waiting-for-approval state onto the floor."""
        if progress.state == "awaiting_approval" and progress.detail != "heartbeat":
            await _send_floor(
                ApprovalPending(
                    agent_id=progress.agent or "team",
                    action=progress.action or "command",
                    summary=progress.detail,
                    lines=list(progress.lines or []),
                    note=progress.note or "",
                    allow_run_scope=bool(progress.allow_run_scope),
                    questions=[
                        {
                            "header": q.header,
                            "question": q.question,
                            "options": list(q.options),
                            "multiple": bool(q.multiple),
                        }
                        for q in (progress.questions or [])
                    ],
                )
            )
        elif progress.state == "approval_resolved":
            await _send_floor(
                ApprovalResolved(
                    agent_id=progress.agent or "team", approved=bool(progress.approved)
                )
            )

    async def _start_pipeline(run) -> None:
        """The full loop on a real run, in the background."""
        from kiln.agents.transcript import TranscriptLog
        from kiln.debug_log import open_run_debug_log
        from kiln.events.schema import TranscriptTick
        from kiln.gateway.client import Gateway
        from kiln.gateway.usage import UsageLog
        from kiln.graph.orchestrator import run_pipeline

        run.status = "building"
        loop = asyncio.get_running_loop()
        debug_root = Path(settings.debug_log_dir) / run.id if settings.debug else None
        debug_log = open_run_debug_log(
            enabled=settings.debug,
            run_id=run.id,
            idea=run.idea,
            workspace=run.workspace or "",
            root=debug_root,
        )
        run.debug_log = debug_log

        def tick(entry) -> None:
            # Fired from gateway worker threads too — hop back to the loop and
            # broadcast a light tick; the drawer fetches the heavy entry itself.
            if debug_log is not None:
                debug_log.append_transcript(entry)
            event = TranscriptTick(agent_id=entry.agent_id, seq=entry.seq, summary=entry.summary())
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(_broadcast(encode_event(event)))
            )

        transcript = TranscriptLog(listener=tick)
        run.transcript = transcript
        # Per-run usage ledger (also mirrored into process-wide via dual listener).
        run_usage = UsageLog(
            listener=(debug_log.append_usage if debug_log is not None else None)
        )

        def mirror_project_usage(record) -> None:
            with app.state.usage._lock:
                app.state.usage.records.append(record)

        if debug_log is not None:
            prev = run_usage.listener

            def both(record) -> None:
                if prev is not None:
                    prev(record)
                mirror_project_usage(record)

            run_usage.listener = both
        else:
            run_usage.listener = mirror_project_usage

        gateway = Gateway(
            settings,
            BudgetGuard(settings.run_token_ceiling),
            run_usage,
            transcript=transcript,
        )

        async def send_floor(event) -> None:
            await _send_floor(event)
            if debug_log is not None:
                debug_log.append_event(event)

        green: bool | None = None
        try:
            green = await run_pipeline(
                run.id,
                run.idea,
                run.user_notes,
                gateway,
                app.state.runs,
                send_floor,
                settings.run_token_ceiling,
                settings,
                transcript=transcript,
            )
            run.status = "green" if green else "failed"
        except Exception:
            logger.exception("pipeline failed for %s", run.id)
            run.status = "failed"
            green = False
        finally:
            if debug_log is not None:
                debug_log.finish(
                    status=run.status,
                    green=green,
                    usage_summary=run_usage.summary(),
                )

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

    def _maybe_start_pipeline(run) -> None:
        """Kick the full loop once a workspace is locked (connect or UI pick)."""
        if not settings.pipeline_autostart:
            return
        if not run.workspace:
            return
        if app.state.pipeline_task is not None and not app.state.pipeline_task.done():
            return
        app.state.pipeline_task = asyncio.create_task(_start_pipeline(run))

    @app.post("/runs")
    async def create_run(request: Request) -> dict:
        """Start a run: returns a one-time pair code. Work begins when the
        local companion claims the code and a workspace is locked
        (docs/local-companion.md)."""
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
            "connect": f"{settings.companion_command_prefix} listen",
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
            "environment": run.environment,
        }

    @app.get("/runs/{run_id}/transcript")
    def run_transcript(run_id: str, agent: str | None = None, after: int = 0) -> dict:
        """The recorded model/tool I/O for one run, fetched when the user clicks
        an agent on the floor. Heavy by design — never broadcast on the SSE floor.
        The API key is never in here: prompts and outputs only."""
        run = app.state.runs.get_run(run_id)
        if run is None:
            return {"error": "unknown run"}
        log = getattr(run, "transcript", None)
        if log is None:
            return {"agents": [], "entries": []}
        if agent:
            entries = log.for_agent(agent, after=after)
        else:
            # without an agent filter, only the roster is cheap enough to serve
            entries = []
        return {
            "agents": log.agents(),
            "entries": [entry.model_dump(mode="json") for entry in entries],
        }

    @app.get("/runs/{run_id}/browse")
    async def browse_run(run_id: str, path: str = "") -> dict:
        """List host directories via the attached companion (UI folder picker)."""
        run = app.state.runs.get_run(run_id)
        if run is None:
            return {"error": "unknown run"}
        if run.status not in ("paired", "ready", "building", "disconnected"):
            return {"error": "companion not attached yet"}
        try:
            raw = await app.state.runs.call_tool(
                run_id, "browse_host", {"path": path or ""}, timeout=30
            )
        except (CompanionUnavailable, PairingError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            listing = json.loads(raw)
        except json.JSONDecodeError:
            return {"ok": False, "error": "companion returned a bad browse payload"}
        return {"ok": True, **listing}

    @app.post("/runs/{run_id}/workspace")
    async def set_run_workspace(run_id: str, request: Request) -> dict:
        """Lock the companion's build folder (after listen pairing) and start work."""
        run = app.state.runs.get_run(run_id)
        if run is None:
            return {"error": "unknown run"}
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
        path = body.get("path", "")
        create = bool(body.get("create", False))
        if not isinstance(path, str) or not path.strip():
            return {"ok": False, "error": "path is required"}
        try:
            raw = await app.state.runs.call_tool(
                run_id,
                "set_workspace",
                {"path": path.strip(), "create": create},
                timeout=30,
            )
        except (CompanionUnavailable, PairingError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            payload = json.loads(raw)
            locked = payload.get("workspace", path.strip())
        except json.JSONDecodeError:
            locked = path.strip()
        app.state.runs.mark_workspace(run_id, str(locked))
        run = app.state.runs.get_run(run_id)
        if run is not None:
            _maybe_start_pipeline(run)
        return {"ok": True, "workspace": str(locked), "status": run.status if run else "ready"}

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

    @app.post("/runs/{run_id}/approvals")
    async def answer_approval(run_id: str, request: Request) -> dict:
        """The floor's answer to a pending shell approval or ask_user questionnaire.
        Relayed to the companion; races the terminal for the same prompt."""
        run = app.state.runs.get_run(run_id)
        if run is None:
            return {"error": "unknown run"}
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
        raw_answers = body.get("answers")
        answers: list[list[str]] | None = None
        if isinstance(raw_answers, list):
            answers = []
            for item in raw_answers:
                if isinstance(item, list):
                    answers.append([str(x) for x in item if str(x).strip()])
                elif isinstance(item, str) and item.strip():
                    answers.append([item.strip()])
                else:
                    answers.append([])
        approved = bool(body.get("approved", answers is not None))
        scope = body.get("scope", "once")
        feedback = body.get("feedback", "")
        if scope not in ("once", "run"):
            scope = "once"
        if not isinstance(feedback, str):
            feedback = ""
        try:
            await app.state.runs.answer_approval(
                run_id,
                approved,
                scope=scope,
                feedback=feedback.strip()[:500],
                answers=answers,
            )
        except CompanionUnavailable as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

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
            connection = app.state.runs.attach(
                run, websocket, message.workspace, message.environment
            )
            from kiln.companion.protocol import HelloAck

            await websocket.send_text(HelloAck(run_id=run.id).model_dump_json())
            logger.info(
                "companion attached to %s (status=%s workspace=%s)",
                run.id,
                run.status,
                run.workspace or "(pending)",
            )
            # connect --dir locks the folder in Hello → ready → start now.
            # listen leaves workspace empty (paired); UI POST /workspace starts later.
            if run.status == "ready":
                _maybe_start_pipeline(run)
            while True:
                payload = await websocket.receive_text()
                reply = decode_companion_message(payload)
                if reply.type == "tool_result":
                    connection.resolve(reply)
                elif reply.type == "progress":
                    connection.touch(reply.id)  # alive: push the call's deadline out
                    await _relay_approval_state(reply)
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
