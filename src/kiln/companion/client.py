"""The companion client: one outbound WebSocket, a dispatch loop.

Receives tool calls from the server, executes them against the scoped
workspace, and returns results. Shell commands never run on the host — only
inside the Docker sandbox via the executor.
"""

import asyncio
import json
import logging

from kiln import __version__
from kiln.companion.executor import Executor
from kiln.companion.protocol import Hello, ToolCall, ToolResult

logger = logging.getLogger("kiln.companion")


async def serve(url: str, code: str, workspace_label: str, executor: Executor) -> int:
    """Connect, handshake, and dispatch tool calls until the socket closes.

    Returns a process-style exit code: 0 clean, 1 pairing refused, 2 unreachable.
    """
    import websockets

    hello = Hello(code=code, workspace=workspace_label, version=__version__)
    try:
        async with websockets.connect(url, max_size=20_000_000) as ws:
            await ws.send(hello.model_dump_json())
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
            message = json.loads(raw)
            if message.get("type") != "hello_ack":
                print(f"pairing refused: {message}")
                return 1
            print(f"connected to run {message['run_id']} — workspace {workspace_label}")
            async for payload in ws:
                reply = _handle(payload, executor)
                if reply is not None:
                    await ws.send(reply.model_dump_json())
            print("server closed the connection")
            return 0
    except websockets.exceptions.InvalidStatus as exc:
        status = getattr(exc.response, "status_code", None)
        print(f"connection refused by the server (HTTP {status})")
        return 1
    except (OSError, TimeoutError) as exc:
        print(f"cannot reach the server at {url}: {exc}")
        return 2


def _handle(payload: str, executor: Executor) -> ToolResult | None:
    try:
        message = json.loads(payload)
    except json.JSONDecodeError:
        return None
    kind = message.get("type")
    if kind == "ping":
        from kiln.companion.protocol import Pong

        return Pong()
    if kind != "tool_call":
        return None
    call = ToolCall.model_validate(message)
    try:
        output = executor.execute(call.tool, call.input)
        return ToolResult(id=call.id, ok=True, output=output)
    except Exception as exc:  # every failure goes back as data, never a crash
        return ToolResult(id=call.id, ok=False, error=f"{type(exc).__name__}: {exc}")
