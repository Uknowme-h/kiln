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


def test_claim_allows_reconnect_after_a_disconnect():
    registry = CompanionRegistry()
    run = registry.create_run("a to-do app")
    registry.claim(run.pair_code)
    registry.attach(run, websocket=None, workspace="/tmp/ws")
    registry.detach(run.id)
    assert run.status == "disconnected"
    assert registry.claim(run.pair_code).id == run.id


def test_call_tool_without_a_companion_is_a_clean_error():
    registry = CompanionRegistry()
    run = registry.create_run("a to-do app")
    with pytest.raises(CompanionUnavailable):
        import asyncio

        asyncio.run(registry.call_tool(run.id, "read_file", {"path": "x"}))


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


class FakeSocket:
    """Records what the registry would send down the websocket."""

    def __init__(self):
        self.sent = []

    async def send_text(self, payload):
        self.sent.append(payload)


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
