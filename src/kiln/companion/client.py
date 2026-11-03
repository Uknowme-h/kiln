"""The companion client: one outbound WebSocket, concurrent tool dispatch.

Receives tool calls from the server, executes each against the scoped
workspace on its own worker thread, and returns results. Calls run
concurrently so two dev agents can work at once; the only serialized thing is
the user's attention (the approval prompt takes one question at a time).

While a call runs the client sends a heartbeat, so the server's inactivity
timer never fires on a long build or on a command waiting for the user's answer.
"""

import asyncio
import json
import logging

from kiln import __version__
from kiln.companion.executor import Executor, PermissionDenied
from kiln.companion.protocol import (
    ApprovalAnswer,
    Hello,
    Pong,
    ToolCall,
    ToolProgress,
    ToolResult,
)

logger = logging.getLogger("kiln.companion")

HEARTBEAT_SECONDS = 5.0


async def serve(url: str, code: str, workspace_label: str, executor: Executor) -> int:
    """Connect, handshake, and dispatch tool calls until the socket closes.

    ``workspace_label`` may be empty (listen mode): the UI picks the folder via
    browse_host / set_workspace before the pipeline starts.

    Returns a process-style exit code: 0 clean, 1 pairing refused, 2 unreachable.
    """
    import websockets

    hello = Hello(
        code=code,
        workspace=workspace_label or "",
        version=__version__,
        environment=executor.environment_report(),
    )
    outbox: asyncio.Queue = asyncio.Queue()
    in_flight: set[asyncio.Task] = set()
    writer: asyncio.Task | None = None
    try:
        async with websockets.connect(url, max_size=20_000_000) as ws:
            await ws.send(hello.model_dump_json())
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
            message = json.loads(raw)
            if message.get("type") != "hello_ack":
                print(f"pairing refused: {message}")
                return 1
            if workspace_label:
                print(f"connected to run {message['run_id']} — workspace {workspace_label}")
            else:
                print(
                    f"paired with run {message['run_id']} — "
                    "waiting for a folder pick in the Kiln UI"
                )
            writer = asyncio.create_task(_writer(ws, outbox))
            try:
                async for payload in ws:
                    task = _dispatch(payload, executor, outbox)
                    if task is not None:
                        in_flight.add(task)
                        task.add_done_callback(in_flight.discard)
            finally:
                for task in list(in_flight):
                    task.cancel()
            print("server closed the connection")
            return 0
    except websockets.exceptions.ConnectionClosedError as exc:
        # e.g. 1012 service restart: the in-memory run died with the server,
        # so this pair code is gone — no point telling the user to re-claim it
        print(
            f"the server dropped the connection ({exc.code} {exc.reason}). "
            "If it restarted, the run went with it — Brief the team again for a new code."
        )
        return 3
    except websockets.exceptions.InvalidStatus as exc:
        status = getattr(exc.response, "status_code", None)
        print(f"connection refused by the server (HTTP {status})")
        return 1
    except (OSError, TimeoutError) as exc:
        print(f"cannot reach the server at {url}: {exc}")
        return 2
    finally:
        if writer is not None:
            writer.cancel()
        executor.close()


async def _writer(ws, outbox: asyncio.Queue) -> None:
    """The only sender: one writer keeps concurrent calls from interleaving frames."""
    try:
        while True:
            message = await outbox.get()
            await ws.send(message.model_dump_json())
    except asyncio.CancelledError:
        raise
    except Exception:  # the socket is gone; the read loop reports why
        return


def _dispatch(payload: str, executor: Executor, outbox: asyncio.Queue) -> asyncio.Task | None:
    try:
        message = json.loads(payload)
    except json.JSONDecodeError:
        return None
    kind = message.get("type")
    if kind == "ping":
        outbox.put_nowait(Pong())
        return None
    if kind == "approval_answer":
        # the web floor answered the pending approval; the prompt resolves it
        # (or drops it if the terminal answered first)
        try:
            answer = ApprovalAnswer.model_validate(message)
        except Exception:
            logger.warning("dropped a malformed approval_answer")
            return None
        if executor.answer_approval(answer):
            print("approval answered from the web floor")
        return None
    if kind != "tool_call":
        return None
    try:
        call = ToolCall.model_validate(message)
    except Exception:
        logger.warning("dropped a malformed tool_call")
        return None
    return asyncio.create_task(_run_call(call, executor, outbox))


async def _run_call(call: ToolCall, executor: Executor, outbox: asyncio.Queue) -> None:
    loop = asyncio.get_running_loop()

    def notify(progress: ToolProgress) -> None:
        # called from the worker thread running the tool
        loop.call_soon_threadsafe(outbox.put_nowait, progress)

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            outbox.put_nowait(ToolProgress(id=call.id, state="working", detail="heartbeat"))

    beat = asyncio.create_task(heartbeat())
    try:
        output = await asyncio.to_thread(
            executor.execute, call.tool, call.input, call.agent, call.id, notify
        )
        result = ToolResult(id=call.id, ok=True, output=output)
        if call.tool == "set_workspace" and result.ok:
            try:
                locked = json.loads(output).get("workspace", "")
            except json.JSONDecodeError:
                locked = output
            print(f"workspace locked — {locked}")
            print("the team can build here now")
    except PermissionDenied as exc:
        result = ToolResult(id=call.id, ok=False, error=str(exc), code="denied")
    except Exception as exc:  # every failure goes back as data, never a crash
        result = ToolResult(id=call.id, ok=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        beat.cancel()
    outbox.put_nowait(result)
