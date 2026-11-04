import time

import pytest

from kiln.companion.registry import (
    CompanionRegistry,
    CompanionUnavailable,
    PairingError,
)


def test_a_run_gets_a_short_unambiguous_pair_code():
    registry = CompanionRegistry()
    run = registry.create_run("a to-do app")
    assert run.id.startswith("run-")
    assert len(run.pair_code) == 6
    assert not (set(run.pair_code) & set("01ILO"))
    assert run.status == "waiting_for_companion"


def test_claim_accepts_the_code_once_and_rejects_strangers():
    registry = CompanionRegistry()
    run = registry.create_run("a to-do app")
    assert registry.claim(run.pair_code.lower()).id == run.id  # case-insensitive
    with pytest.raises(PairingError):
        registry.claim("ZZZZZZ")


def test_claim_rejects_expired_codes():
    registry = CompanionRegistry()
    run = registry.create_run("a to-do app")
    run.created_at = time.time() - 20 * 60
    with pytest.raises(PairingError):
        registry.claim(run.pair_code)


class FakeSocket:
    """Records what the registry would send down the websocket."""

    def __init__(self):
        self.sent = []

    async def send_text(self, payload):
        self.sent.append(payload)


def test_claim_allows_reconnect_after_a_disconnect():
    registry = CompanionRegistry()
    run = registry.create_run("a to-do app")
    registry.claim(run.pair_code)
    registry.attach(run, websocket=None, workspace="/tmp/ws")
    registry.detach(run.id)
    assert run.status == "disconnected"
    assert registry.claim(run.pair_code).id == run.id


def test_attach_without_workspace_is_paired_until_mark():
    registry = CompanionRegistry()
    run = registry.create_run("a to-do app")
    registry.claim(run.pair_code)
    registry.attach(run, websocket=None, workspace="")
    assert run.status == "paired"
    assert run.workspace == ""
    registry.mark_workspace(run.id, "/tmp/picked")
    assert run.status == "ready"
    assert run.workspace == "/tmp/picked"


def test_agent_tools_are_blocked_while_paired():
    import asyncio

    registry = CompanionRegistry()
    run = registry.create_run("a to-do app")
    registry.claim(run.pair_code)
    registry.attach(run, websocket=FakeSocket(), workspace="")
    with pytest.raises(CompanionUnavailable, match="workspace not chosen"):
        asyncio.run(registry.call_tool(run.id, "read_file", {"path": "x"}))


def test_call_tool_without_a_companion_is_a_clean_error():
    registry = CompanionRegistry()
    run = registry.create_run("a to-do app")
    with pytest.raises(CompanionUnavailable):
        import asyncio

        # short timeout: no companion ever attaches, so it gives up cleanly
        asyncio.run(registry.call_tool(run.id, "read_file", {"path": "x"}, timeout=0.05))


def test_call_tool_waits_out_a_transient_drop_and_resumes_on_reconnect():
    """The run-ff82d6e6 gap: a drop mid-build used to fail the run on the spot.
    Now call_tool waits for the companion to re-attach instead of failing."""
    import asyncio
    import json

    from kiln.companion.protocol import ToolResult

    registry = CompanionRegistry()
    run = registry.create_run("a to-do app")
    registry.claim(run.pair_code)
    registry.attach(run, websocket=FakeSocket(), workspace="/tmp/ws")
    registry.detach(run.id)  # companion drops before any call lands

    async def scenario():
        task = asyncio.create_task(
            registry.call_tool(run.id, "read_file", {"path": "a.txt"}, timeout=20)
        )
        await asyncio.sleep(0.1)  # the call is now waiting for an attach
        # the companion watchdog re-claims with the same code
        registry.claim(run.pair_code)
        socket2 = FakeSocket()
        registry.attach(run, websocket=socket2, workspace="/tmp/ws")
        await asyncio.sleep(0)  # let the re-sent call go out
        call = json.loads(socket2.sent[0])
        registry.connections[run.id].resolve(
            ToolResult(id=call["id"], ok=True, output="after reconnect")
        )
        return await task

    assert asyncio.run(scenario()) == "after reconnect"


def test_detach_fails_calls_in_flight():
    import asyncio

    registry = CompanionRegistry()
    run = registry.create_run("a to-do app")
    registry.claim(run.pair_code)
    connection = registry.attach(run, websocket=None, workspace="/tmp/ws")

    async def scenario():
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        connection.pending["call-1"] = future
        registry.detach(run.id)
        with pytest.raises(CompanionUnavailable):
            await future

    asyncio.run(scenario())


def test_call_tool_sends_the_call_and_resolves_on_the_result():
    import asyncio
    import json

    from kiln.companion.protocol import ToolResult

    registry = CompanionRegistry()
    run = registry.create_run("a to-do app")
    registry.claim(run.pair_code)
    socket = FakeSocket()
    registry.attach(run, websocket=socket, workspace="/tmp/ws")

    async def scenario():
        task = asyncio.create_task(
            registry.call_tool(run.id, "read_file", {"path": "a.txt"}, timeout=5)
        )
        await asyncio.sleep(0)  # let the call go out
        call = json.loads(socket.sent[0])
        assert call["type"] == "tool_call"
        assert call["tool"] == "read_file"
        assert call["input"] == {"path": "a.txt"}
        connection = registry.connections[run.id]
        connection.resolve(ToolResult(id=call["id"], ok=True, output="contents"))
        return await task

    assert asyncio.run(scenario()) == "contents"


def test_call_tool_surfaces_companion_errors_and_timeouts():
    import asyncio
    import json

    from kiln.companion.protocol import ToolResult

    registry = CompanionRegistry()
    run = registry.create_run("a to-do app")
    registry.claim(run.pair_code)
    socket = FakeSocket()
    registry.attach(run, websocket=socket, workspace="/tmp/ws")

    async def error_case():
        task = asyncio.create_task(
            registry.call_tool(run.id, "write_file", {"path": "a", "content": "b"}, timeout=5)
        )
        await asyncio.sleep(0)
        call = json.loads(socket.sent[0])
        registry.connections[run.id].resolve(
            ToolResult(id=call["id"], ok=False, error="disk full")
        )
        with pytest.raises(CompanionUnavailable, match="disk full"):
            await task

    async def timeout_case():
        with pytest.raises(CompanionUnavailable, match="in time"):
            await registry.call_tool(run.id, "run_tests", {}, timeout=0.05)

    asyncio.run(error_case())
    asyncio.run(timeout_case())


def test_resolve_from_another_thread_wakes_the_waiting_loop():
    import asyncio
    import json
    import threading

    from kiln.companion.protocol import ToolResult

    registry = CompanionRegistry()
    run = registry.create_run("a to-do app")
    registry.claim(run.pair_code)
    socket = FakeSocket()
    registry.attach(run, websocket=socket, workspace="/tmp/ws")

    async def scenario():
        task = asyncio.create_task(
            registry.call_tool(run.id, "read_file", {"path": "a.txt"}, timeout=5)
        )
        await asyncio.sleep(0)
        call = json.loads(socket.sent[0])
        # resolve from a foreign thread, as a multi-worker server would
        threading.Thread(
            target=lambda: registry.connections[run.id].resolve(
                ToolResult(id=call["id"], ok=True, output="cross-thread")
            )
        ).start()
        return await task

    assert asyncio.run(scenario()) == "cross-thread"


def test_notes_queue_in_order_per_run():
    registry = CompanionRegistry()
    run = registry.create_run("a to-do app")
    assert registry.add_note(run.id, "first") == 1
    assert registry.add_note(run.id, "second") == 2
    assert run.user_notes == ["first", "second"]


def test_claims_are_tracked_and_replayed_on_reattach():
    """The server is the source of truth for who owns what mid-build. After a
    companion drop + re-attach, replay_claims re-issues active claim_scope so
    the respawned companion's LeaseTable matches (run-d918940d)."""
    import asyncio
    import json

    from kiln.companion.protocol import AgentScope, ToolResult

    registry = CompanionRegistry()
    run = registry.create_run("a to-do app")
    registry.claim(run.pair_code)
    socket = FakeSocket()
    registry.attach(run, websocket=socket, workspace="/tmp/ws")

    async def scenario():
        # planner claims docs/ (via a relayed tool call)
        scope = AgentScope(id="planner", label="Pat", role="planner", owned_paths=["docs/"]).model_dump()
        task = asyncio.create_task(
            registry.call_tool(
                run.id, "claim_scope",
                {"owned_paths": ["docs/"], "task": "plan", "status": "working"},
                timeout=5, agent=scope,
            )
        )
        await asyncio.sleep(0)
        call = json.loads(socket.sent[0])
        assert call["tool"] == "claim_scope"
        registry.connections[run.id].resolve(ToolResult(id=call["id"], ok=True, output="leased"))
        await task
        assert "planner" in (registry._claims.get(run.id) or {})

        # companion drops and re-attaches (fresh socket)
        registry.detach(run.id)
        registry.claim(run.pair_code)
        socket2 = FakeSocket()
        registry.attach(run, websocket=socket2, workspace="/tmp/ws")

        # the replay re-issues the planner's claim on the new connection
        replay_task = asyncio.create_task(registry.replay_claims(run.id))
        await asyncio.sleep(0)
        replay_call = json.loads(socket2.sent[0])
        assert replay_call["tool"] == "claim_scope"
        assert replay_call["input"]["owned_paths"] == ["docs/"]
        registry.connections[run.id].resolve(
            ToolResult(id=replay_call["id"], ok=True, output="leased")
        )
        return await replay_task

    assert asyncio.run(scenario()) == 1


def test_a_long_build_can_reconnect_after_the_pair_ttl():
    """The 15-minute TTL guards the first pairing only — a drop at minute 20 of
    a build must still be able to reconnect."""
    registry = CompanionRegistry()
    run = registry.create_run("a to-do app")
    registry.claim(run.pair_code)
    registry.attach(run, websocket=FakeSocket(), workspace="/tmp/ws")
    run.status = "building"
    run.created_at = time.time() - 20 * 60
    registry.detach(run.id)
    assert registry.claim(run.pair_code).id == run.id


def test_a_reconnect_takes_over_a_stale_socket_without_being_evicted():
    """Reconnect before the server noticed the old socket died: the code is
    accepted, the new connection wins, and the old handler's late detach must
    not evict it."""
    registry = CompanionRegistry()
    run = registry.create_run("a to-do app")
    registry.claim(run.pair_code)
    old = registry.attach(run, websocket=FakeSocket(), workspace="/tmp/ws")
    run.status = "building"

    assert registry.claim(run.pair_code).id == run.id  # not "already claimed"
    new = registry.attach(run, websocket=FakeSocket(), workspace="/tmp/ws")
    registry.detach(run.id, old)  # the stale handler's finally block
    assert registry.connections[run.id] is new
    assert run.status == "building"


def test_an_interrupted_idempotent_call_is_retried_after_reconnect():
    import asyncio
    import json

    from kiln.companion.protocol import ToolResult

    registry = CompanionRegistry()
    run = registry.create_run("a to-do app")
    registry.claim(run.pair_code)
    registry.attach(run, websocket=FakeSocket(), workspace="/tmp/ws")
    run.status = "building"

    async def scenario():
        task = asyncio.create_task(
            registry.call_tool(run.id, "read_file", {"path": "a.txt"}, timeout=20)
        )
        await asyncio.sleep(0)
        registry.detach(run.id)  # the socket dies with the read in flight
        await asyncio.sleep(0.05)
        registry.claim(run.pair_code)
        socket2 = FakeSocket()
        registry.attach(run, websocket=socket2, workspace="/tmp/ws")
        for _ in range(50):
            if socket2.sent:
                break
            await asyncio.sleep(0.01)
        call = json.loads(socket2.sent[0])
        assert call["tool"] == "read_file"  # the same read, replayed
        registry.connections[run.id].resolve(ToolResult(id=call["id"], ok=True, output="data"))
        return await task

    assert asyncio.run(scenario()) == "data"


def test_an_interrupted_write_is_never_replayed():
    import asyncio

    registry = CompanionRegistry()
    run = registry.create_run("a to-do app")
    registry.claim(run.pair_code)
    registry.attach(run, websocket=FakeSocket(), workspace="/tmp/ws")

    async def scenario():
        task = asyncio.create_task(
            registry.call_tool(run.id, "write_file", {"path": "a", "content": "b"}, timeout=20)
        )
        await asyncio.sleep(0)
        registry.detach(run.id)
        with pytest.raises(CompanionUnavailable, match="disconnected"):
            await task

    asyncio.run(scenario())


def test_release_removes_the_claim_from_the_replay_set():
    import asyncio
    import json

    from kiln.companion.protocol import AgentScope, ToolResult

    registry = CompanionRegistry()
    run = registry.create_run("a to-do app")
    registry.claim(run.pair_code)
    socket = FakeSocket()
    registry.attach(run, websocket=socket, workspace="/tmp/ws")

    async def scenario():
        scope = AgentScope(id="planner", label="Pat", role="planner", owned_paths=["docs/"]).model_dump()
        for tool, inp in (
            ("claim_scope", {"owned_paths": ["docs/"], "task": "plan"}),
            ("release_scope", {}),
        ):
            task = asyncio.create_task(
                registry.call_tool(run.id, tool, inp, timeout=5, agent=scope)
            )
            await asyncio.sleep(0)
            call = json.loads(socket.sent[-1])
            registry.connections[run.id].resolve(ToolResult(id=call["id"], ok=True, output="ok"))
            await task
        assert (registry._claims.get(run.id) or {}) == {}
        return await registry.replay_claims(run.id)

    assert asyncio.run(scenario()) == 0
