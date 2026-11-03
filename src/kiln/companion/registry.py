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


class ToolFailed(CompanionUnavailable):
    """The companion ran the tool and it failed. The message is safe to show the model."""


class ToolDenied(ToolFailed):
    """Policy or the user refused the call. The message carries any feedback they typed."""


# Control tools allowed while status is ``paired`` (no workspace yet).
_PAIRED_TOOLS = frozenset({"browse_host", "set_workspace"})


@dataclass
class Run:
    id: str
    idea: str
    pair_code: str
    # waiting_for_companion | paired | ready | disconnected | building | green | failed
    status: str = "waiting_for_companion"
    workspace: str = ""
    user_notes: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    # what the companion reported about the machine: OS, shell, runtimes on PATH
    environment: dict = field(default_factory=dict)
    # the run's recorded model/tool I/O, attached when the pipeline starts;
    # served by GET /runs/{id}/transcript when the user clicks an agent
    transcript: object = None
    # optional RunDebugLog when Settings.debug is on
    debug_log: object = None


class CompanionConnection:
    """One attached companion: a websocket plus the calls in flight.

    A call's timeout is an inactivity timeout: the companion sends a progress
    message while it works (and while it waits for the user to approve a
    command), and each one pushes the deadline out again.
    """

    def __init__(self, websocket):
        self.websocket = websocket
        self.pending: dict[str, asyncio.Future] = {}
        self._deadlines: dict[str, float] = {}
        self._windows: dict[str, float] = {}

    def touch(self, call_id: str) -> None:
        """A progress message arrived: the call is alive, so extend its deadline."""
        window = self._windows.get(call_id)
        if window is not None:
            self._deadlines[call_id] = time.monotonic() + window

    async def call(
        self, tool: str, tool_input: dict, timeout: float, agent: dict | None = None
    ) -> str:
        from kiln.companion.protocol import ToolCall

        call_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending[call_id] = future
        self._windows[call_id] = timeout
        self._deadlines[call_id] = time.monotonic() + timeout
        try:
            message = ToolCall(id=call_id, tool=tool, input=tool_input, agent=agent)
            await self.websocket.send_text(message.model_dump_json())
            while True:
                remaining = self._deadlines[call_id] - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                try:
                    result = await asyncio.wait_for(asyncio.shield(future), timeout=remaining)
                    break
                except TimeoutError:
                    continue  # progress may have pushed the deadline out meanwhile
        except TimeoutError:
            raise CompanionUnavailable(f"companion did not answer {tool} in time") from None
        finally:
            self.pending.pop(call_id, None)
            self._deadlines.pop(call_id, None)
            self._windows.pop(call_id, None)
        if not result.ok:
            message_text = result.error or f"{tool} failed on the companion"
            if result.code == "denied":
                raise ToolDenied(message_text)
            raise ToolFailed(message_text)
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

    def attach(
        self, run: Run, websocket, workspace: str, environment: dict | None = None
    ) -> CompanionConnection:
        connection = CompanionConnection(websocket)
        self.connections[run.id] = connection
        chosen = (workspace or "").strip()
        if chosen:
            run.workspace = chosen
            run.status = "ready"
        elif run.workspace:
            # reconnect after a drop: keep the folder locked earlier
            run.status = "ready"
        else:
            run.workspace = ""
            run.status = "paired"
        run.environment = dict(environment or {})
        return connection

    def mark_workspace(self, run_id: str, workspace: str) -> Run:
        """Flip a paired run to ready after the companion locks a folder."""
        run = self.runs.get(run_id)
        if run is None:
            raise PairingError("unknown run")
        path = (workspace or "").strip()
        if not path:
            raise PairingError("workspace path is required")
        run.workspace = path
        if run.status in ("paired", "waiting_for_companion", "disconnected", "ready"):
            run.status = "ready"
        return run

    def detach(self, run_id: str) -> None:
        connection = self.connections.pop(run_id, None)
        if connection is not None:
            connection.fail_all("companion disconnected")
        run = self.runs.get(run_id)
        if run is not None and run.status in ("paired", "ready"):
            run.status = "disconnected"

    async def call_tool(
        self,
        run_id: str,
        tool: str,
        tool_input: dict,
        timeout: float = CALL_TIMEOUT_SECONDS,
        agent: dict | None = None,
    ) -> str:
        connection = self.connections.get(run_id)
        if connection is None:
            raise CompanionUnavailable("no companion attached to this run")
        run = self.runs.get(run_id)
        if run is not None and run.status == "paired" and tool not in _PAIRED_TOOLS:
            raise CompanionUnavailable(
                "workspace not chosen yet — only browse_host and set_workspace are allowed"
            )
        return await connection.call(tool, tool_input, timeout, agent)

    async def answer_approval(
        self,
        run_id: str,
        approved: bool = True,
        scope: str = "once",
        feedback: str = "",
        answers: list[list[str]] | None = None,
    ) -> None:
        """Relay the web floor's approval or ask_user reply to the companion,
        where it races the terminal prompt for the same question."""
        from kiln.companion.protocol import ApprovalAnswer

        connection = self.connections.get(run_id)
        if connection is None:
            raise CompanionUnavailable("no companion attached to this run")
        answer = ApprovalAnswer(
            approved=approved, scope=scope, feedback=feedback, answers=answers
        )
        await connection.websocket.send_text(answer.model_dump_json())

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
