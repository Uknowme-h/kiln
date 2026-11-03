"""Persisted run debug logs (DEBUG=true)."""

from pathlib import Path

from kiln.agents.transcript import TranscriptLog
from kiln.debug_log import RunDebugLog, open_run_debug_log
from kiln.gateway.routing import Route
from kiln.gateway.usage import UsageLog
from kiln.agents.roles import Role


def test_open_run_debug_log_disabled_is_none(tmp_path: Path):
    assert open_run_debug_log(enabled=False, run_id="r1", root=tmp_path / "r1") is None


def test_persists_transcript_usage_events_and_summary(tmp_path: Path):
    root = tmp_path / "run-abc"
    log = RunDebugLog("run-abc", root=root, idea="a todo app", workspace="/tmp/proj")

    transcript = TranscriptLog(listener=log.append_transcript)
    transcript.record(
        "worker-1",
        "worker",
        "model_call",
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "content": "hi"},
    )
    transcript.record(
        "worker-1",
        "worker",
        "tool_call",
        {"name": "shell", "args": {"command": "pytest"}},
    )

    usage = UsageLog(listener=log.append_usage)
    usage.record(
        role=Role.WORKER,
        route=Route(
            role=Role.WORKER,
            provider="meta",
            model="spark",
            base_url="x",
            api_key="k",
            fallback=False,
        ),
        prompt_tokens=10,
        completion_tokens=5,
        agent_id="worker-1",
    )

    log.append_event({"kind": "agent_status", "detail": "coding"})
    summary_path = log.finish(status="green", green=True, usage_summary=usage.summary())

    assert summary_path.is_file()
    assert (root / "meta.json").is_file()
    assert (root / "transcript.jsonl").read_text(encoding="utf-8").count("\n") == 2
    assert (root / "usage.jsonl").read_text(encoding="utf-8").count("\n") == 1
    assert (root / "events.jsonl").read_text(encoding="utf-8").count("\n") == 1

    import json

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["green"] is True
    assert summary["usage"]["calls"] == 1
    assert summary["usage"]["by_agent"]["worker-1"]["prompt_tokens"] == 10
    assert summary["usage"]["by_role"]["worker"]["completion_tokens"] == 5


def test_usage_summary_includes_by_agent():
    usage = UsageLog()
    usage.record(
        role=Role.PLANNER,
        route=Route(
            role=Role.PLANNER,
            provider="meta",
            model="spark",
            base_url="x",
            api_key="k",
            fallback=False,
        ),
        prompt_tokens=3,
        completion_tokens=1,
        agent_id="planner",
    )
    summary = usage.summary()
    assert summary["by_agent"]["planner"]["calls"] == 1
