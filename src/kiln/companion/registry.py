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

# Safe to re-run after a disconnect interrupted them mid-flight: reads and
# scope bookkeeping. Writes, edits and shell are never replayed — a partial
# write or a half-run command is not idempotent.
_IDEMPOTENT_TOOLS = frozenset(
    {"read_file", "list_dir", "glob", "grep", "claim_scope", "release_scope", "list_peers"}
)
_DISCONNECT_REASONS = ("companion disconnected", "companion reconnected")


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
    # the companion auto-approves command prompts (--auto-approve or the floor)
    auto_approve: bool = False


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
        # Signalled whenever a companion attaches, so an in-flight call that
        # found no companion can wait out a transient drop instead of failing.
        self._attach_event = asyncio.Event()
        # Active claim_scope per run: agent_id -> {owned_paths, label, task}.
        # The server is the source of truth for who owns what mid-build; on a
        # companion re-attach these are replayed so the respawned companion's
        # LeaseTable matches (the run-d918940d LeaseError fix).
        self._claims: dict[str, dict[str, dict]] = {}

    def _track_claim(self, run_id: str, tool_input: dict, agent: dict | None) -> None:
        if not agent or not agent.get("id"):
            return
        owned = list(tool_input.get("owned_paths") or agent.get("owned_paths") or [])
        if not owned:
            return
        self._claims.setdefault(run_id, {})[agent["id"]] = {
            "owned_paths": owned,
            "label": agent.get("label") or agent["id"],
            "task": str(tool_input.get("task") or ""),
            "status": str(tool_input.get("status") or "working"),
        }

    def _track_release(self, run_id: str, agent: dict | None) -> None:
        if agent and agent.get("id") and run_id in self._claims:
            self._claims[run_id].pop(agent["id"], None)

    async def replay_claims(self, run_id: str) -> int:
        """Re-issue active claim_scope calls to a (re)attached companion.

        Called by main after a companion reconnects mid-build. Without this the
        respawned companion has an empty LeaseTable and refuses every in-scope
        write (run-d918940d). Returns how many claims were replayed."""
        claims = list((self._claims.get(run_id) or {}).items())
        if not claims:
            return 0
        from kiln.companion.protocol import AgentScope

        replayed = 0
        for agent_id, info in claims:
            scope = AgentScope(
                id=agent_id,
                label=info["label"],
                role="",
                owned_paths=list(info["owned_paths"]),
            ).model_dump()
            try:
                await self.call_tool(
                    run_id,
                    "claim_scope",
                    {
                        "owned_paths": list(info["owned_paths"]),
                        "task": info["task"],
                        "status": info["status"],
                    },
                    timeout=30,
                    agent=scope,
                )
                replayed += 1
            except CompanionUnavailable:
                break  # companion dropped again mid-replay; the wait handles it
        return replayed

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
        if run.status == "waiting_for_companion":
            # the TTL guards the FIRST pairing only; a build that runs past
            # 15 minutes must still be able to reconnect after a drop
            if time.time() - run.created_at > PAIR_TTL_SECONDS:
                raise PairingError("pair code expired")
            return run
        if run.status == "disconnected":
            return run
        # Reconnect before the server noticed the old socket died: the same
        # code re-attaching to a run whose folder is already locked replaces
        # the stale connection (attach + connection-aware detach handle it).
        if run.workspace and run.status in ("ready", "building", "paired"):
            return run
        raise PairingError("pair code already claimed")

    def attach(
        self,
        run: Run,
        websocket,
        workspace: str,
        environment: dict | None = None,
        auto_approve: bool | None = None,
    ) -> CompanionConnection:
        stale = self.connections.get(run.id)
        if stale is not None:
            # a reconnect replaced a socket the server had not noticed was dead;
            # its in-flight calls cannot complete on it
            stale.fail_all("companion reconnected")
        connection = CompanionConnection(websocket)
        self.connections[run.id] = connection
        chosen = (workspace or "").strip()
        if chosen:
            run.workspace = chosen
        elif not run.workspace:
            run.workspace = ""
        # A reconnect mid-build resumes as ready; a fresh pair with no folder
        # yet waits as paired. Never demote a run that is already building.
        if run.status in ("disconnected", "waiting_for_companion"):
            run.status = "ready" if run.workspace else "paired"
        run.environment = dict(environment or {})
        if auto_approve is not None:
            run.auto_approve = bool(auto_approve)
        # wake any call_tool waiting out a transient drop for this run
        self._attach_event.set()
        self._attach_event = asyncio.Event()
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

    def detach(self, run_id: str, connection: CompanionConnection | None = None) -> None:
        current = self.connections.get(run_id)
        if connection is not None and current is not connection:
            # an old socket's handler finishing after a reconnect took over:
            # fail only its own calls, never evict the live connection
            connection.fail_all("companion disconnected")
            return
        self.connections.pop(run_id, None)
        if current is not None:
            current.fail_all("companion disconnected")
        run = self.runs.get(run_id)
        # Any drop lets the companion re-claim with the same code — including a
        # drop mid-'building' (the run's model state lives server-side, so the
        # pipeline can resume once the workspace is reachable again). Before
        # this, only paired/ready flipped to disconnected, so a companion that
        # died during a build could never reconnect ("pair code already
        # claimed") and the run failed with CompanionUnavailable.
        if run is not None and run.status in ("paired", "ready", "building"):
            run.status = "disconnected"

    async def call_tool(
        self,
        run_id: str,
        tool: str,
        tool_input: dict,
        timeout: float = CALL_TIMEOUT_SECONDS,
        agent: dict | None = None,
    ) -> str:
        run = self.runs.get(run_id)
        if run is not None and run.status == "paired" and tool not in _PAIRED_TOOLS:
            raise CompanionUnavailable(
                "workspace not chosen yet — only browse_host and set_workspace are allowed"
            )
        # A call that finds no companion waits out a transient drop rather than
        # failing the run on the spot. (An in-flight call lost to a disconnect
        # is *not* blindly retried — re-running a partial write/shell is not
        # idempotent. The orchestrator's fixer/plan loop re-drives it cleanly.)
        connection = self.connections.get(run_id)
        if connection is None:
            connection = await self._await_attach(run_id, timeout)
        try:
            output = await connection.call(tool, tool_input, timeout, agent)
        except CompanionUnavailable as exc:
            # ToolFailed/ToolDenied are real answers, never retried
            interrupted = type(exc) is CompanionUnavailable and str(exc) in _DISCONNECT_REASONS
            if not (interrupted and tool in _IDEMPOTENT_TOOLS):
                raise
            connection = await self._await_attach(run_id, timeout)
            output = await connection.call(tool, tool_input, timeout, agent)
        # Track lease state as the source of truth for the re-attach replay.
        # Only on success — a failed claim/release changes nothing.
        if tool == "claim_scope":
            self._track_claim(run_id, tool_input, agent)
        elif tool == "release_scope":
            self._track_release(run_id, agent)
        return output

    async def _await_attach(self, run_id: str, timeout: float) -> CompanionConnection:
        """Wait for a companion to (re)attach within `timeout`, else CompanionUnavailable."""
        deadline = time.monotonic() + min(timeout, 90)
        while time.monotonic() < deadline:
            connection = self.connections.get(run_id)
            if connection is not None:
                return connection
            event = self._attach_event
            try:
                await asyncio.wait_for(event.wait(), timeout=5)
            except TimeoutError:
                pass
        raise CompanionUnavailable("no companion attached to this run")

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
        if scope == "all" and approved and answers is None:
            self.runs[run_id].auto_approve = True

    async def set_auto_approve(self, run_id: str, enabled: bool) -> None:
        """Flip the companion's auto-approve for command prompts from the floor."""
        from kiln.companion.protocol import AutoApprove

        connection = self.connections.get(run_id)
        if connection is None:
            raise CompanionUnavailable("no companion attached to this run")
        await connection.websocket.send_text(AutoApprove(enabled=enabled).model_dump_json())
        self.runs[run_id].auto_approve = bool(enabled)

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
