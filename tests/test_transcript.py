"""Transcript store: per-agent recording, filtering, clipping, and the cap."""

from kiln.agents.transcript import FIELD_CLIP, MAX_ENTRIES, TranscriptLog


def test_records_and_filters_per_agent():
    log = TranscriptLog()
    log.record("worker-1", "worker", "tool_call", {"name": "shell", "args": {}})
    log.record("planner", "planner", "model_call", {"content": "{}"})
    log.record("worker-1", "worker", "tool_result", {"name": "shell", "output": "ok"})

    assert log.agents() == ["worker-1", "planner"]
    mine = log.for_agent("worker-1")
    assert [entry.kind for entry in mine] == ["tool_call", "tool_result"]
    assert mine[0].seq == 1
    assert mine[1].seq == 3


def test_after_returns_only_newer_entries():
    log = TranscriptLog()
    for i in range(4):
        log.record("fixer", "fixer", "note", {"text": f"n{i}"})
    fresh = log.for_agent("fixer", after=2)
    assert [entry.seq for entry in fresh] == [3, 4]


def test_long_fields_are_clipped():
    log = TranscriptLog()
    log.record("worker-1", "worker", "tool_result", {"output": "x" * (FIELD_CLIP + 500)})
    stored = log.for_agent("worker-1")[0].payload["output"]
    assert len(stored) < FIELD_CLIP + 500
    assert "clipped" in stored


def test_the_cap_drops_oldest_but_keeps_recording():
    log = TranscriptLog()
    for i in range(MAX_ENTRIES + 10):
        log.record("worker-2", "worker", "note", {"text": str(i)})
    entries = log.for_agent("worker-2")
    assert len(entries) == MAX_ENTRIES
    assert entries[-1].payload["text"] == str(MAX_ENTRIES + 9)


def test_a_throwing_listener_never_breaks_recording():
    def bad(_entry):
        raise RuntimeError("boom")

    log = TranscriptLog(listener=bad)
    entry = log.record("planner", "planner", "note", {"text": "hi"})
    assert entry.seq == 1


def test_listener_receives_each_entry():
    seen = []
    log = TranscriptLog(listener=seen.append)
    log.record("reporter", "reporter", "model_call", {"content": "x"})
    assert len(seen) == 1
    assert seen[0].agent_id == "reporter"


def test_summary_lines():
    log = TranscriptLog()
    model = log.record(
        "planner", "planner", "model_call",
        {"content": "{}", "total_tokens": 1200, "tool_calls": []},
    )
    assert "1,200" in model.summary()
    call = log.record("worker-1", "worker", "tool_call", {"name": "shell", "args": {}})
    assert call.summary() == "called shell"
    err = log.record("worker-1", "worker", "tool_result", {"name": "shell", "error": "denied"})
    assert "✗" in err.summary()
    truncated = log.record(
        "planner", "planner", "model_call",
        {"content": "", "truncated": True, "tool_calls": []},
    )
    assert "truncated" in truncated.summary()
