"""Server-side registry of runs and their attached companions.

A run is created with a short one-time pair code. The companion claims the
code over the WebSocket, and from then on `call_tool` relays tool calls to
that machine and awaits the matching result. A disconnect pauses the run; it
does not fail it.
"""

import asyncio
import secrets
import time
import uuid
from dataclasses import dataclass, field

PAIR_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no look-alikes
PAIR_TTL_SECONDS = 15 * 60
CALL_TIMEOUT_SECONDS = 120


class PairingError(RuntimeError):
    """The pair code is unknown, expired, or already claimed."""


class CompanionUnavailable(RuntimeError):
    """No companion is attached to the run, or the call timed out."""


@dataclass
class Run:
    id: str
    idea: str
    pair_code: str
    status: str = "waiting_for_companion"  # waiting | ready | disconnected
    workspace: str = ""
    user_notes: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


class CompanionConnection:
    """One attached companion: a websocket plus the calls in flight."""

    def __init__(self, websocket):
        self.websocket = websocket
        self.pending: dict[str, asyncio.Future] = {}

    async def call(self, tool: str, tool_input: dict, timeout: float) -> str:
        from kiln.companion.protocol import ToolCall

        call_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending[call_id] = future
        try:
            message = ToolCall(id=call_id, tool=tool, input=tool_input)
            await self.websocket.send_text(message.model_dump_json())
            result = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            raise CompanionUnavailable(f"companion did not answer {tool} in time") from None
        finally:
            self.pending.pop(call_id, None)
        if not result.ok:
            raise CompanionUnavailable(result.error or f"{tool} failed on the companion")
        return result.output

    @staticmethod
    def _set_result(future: asyncio.Future, result) -> None:
        if not future.done():
            future.set_result(result)

    @staticmethod
    def _set_exception(future: asyncio.Future, exc: Exception) -> None:
        if not future.done():
            future.set_exception(exc)

    def resolve(self, result) -> None:
        """Resolve the waiting call. Safe from any loop: results arrive on the
        websocket handler's loop while the caller may wait on another."""
        future = self.pending.pop(result.id, None)
        if future is None:
            return
        future.get_loop().call_soon_threadsafe(self._set_result, future, result)

    def fail_all(self, reason: str) -> None:
        for future in self.pending.values():
            future.get_loop().call_soon_threadsafe(
                self._set_exception, future, CompanionUnavailable(reason)
            )
        self.pending.clear()


class CompanionRegistry:
    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}
        self.connections: dict[str, CompanionConnection] = {}
        self._codes: dict[str, str] = {}  # pair_code -> run_id

    def create_run(self, idea: str) -> Run:
        code = "".join(secrets.choice(PAIR_CODE_ALPHABET) for _ in range(6))
        run = Run(id=f"run-{uuid.uuid4().hex[:8]}", idea=idea, pair_code=code)
        self.runs[run.id] = run
        self._codes[code] = run.id
        return run

    def get_run(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)

    def claim(self, code: str) -> Run:
        run_id = self._codes.get(code.strip().upper())
        run = self.runs.get(run_id or "")
        if run is None:
            raise PairingError("unknown pair code")
        # a disconnected companion may reconnect with the same code
        if run.status not in ("waiting_for_companion", "disconnected"):
            raise PairingError("pair code already claimed")
        if time.time() - run.created_at > PAIR_TTL_SECONDS:
            raise PairingError("pair code expired")
        return run

    def attach(self, run: Run, websocket, workspace: str) -> CompanionConnection:
        connection = CompanionConnection(websocket)
        self.connections[run.id] = connection
        run.status = "ready"
        run.workspace = workspace
        return connection

    def detach(self, run_id: str) -> None:
        connection = self.connections.pop(run_id, None)
        if connection is not None:
            connection.fail_all("companion disconnected")
        run = self.runs.get(run_id)
        if run is not None and run.status == "ready":
            run.status = "disconnected"

    async def call_tool(
        self,
        run_id: str,
        tool: str,
        tool_input: dict,
        timeout: float = CALL_TIMEOUT_SECONDS,
    ) -> str:
        connection = self.connections.get(run_id)
        if connection is None:
            raise CompanionUnavailable("no companion attached to this run")
        return await connection.call(tool, tool_input, timeout)

    def add_note(self, run_id: str, text: str) -> int:
        run = self.runs[run_id]
        run.user_notes.append(text)
        return len(run.user_notes)

    def expire_stale_codes(self) -> int:
        now = time.time()
        stale = [
            code for code, run_id in self._codes.items()
            if self.runs[run_id].status == "waiting_for_companion"
            and now - self.runs[run_id].created_at > PAIR_TTL_SECONDS
        ]
        for code in stale:
            del self._codes[code]
        return len(stale)
