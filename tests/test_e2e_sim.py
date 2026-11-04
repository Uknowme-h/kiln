"""The e2e simulation tier: every scenario, plus negative controls.

Run with `pytest -m sim` (excluded from the default run to keep it fast).

A scenario that cannot fail proves nothing, so each negative control puts one
fixed bug back, in-process, and asserts the scenario that guards it now FAILS
— with the failure we expect, not some incidental one.
"""

import types

import pytest

from kiln.simulation import SCENARIOS, run_scenario

pytestmark = pytest.mark.sim


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_scenario(name, tmp_path):
    result = run_scenario(SCENARIOS[name], tmp_path / "ws")
    assert not result.failures, f"{name}: {result.failures}"


# ── negative controls: each guard must catch its bug when the fix is removed ──


def test_control_auto_approve_skipping_the_plan_is_caught(monkeypatch, tmp_path):
    from kiln.companion import permissions

    monkeypatch.setattr(permissions, "NEVER_AUTO_APPROVE", frozenset())
    result = run_scenario(SCENARIOS["happy"], tmp_path / "ws")
    assert any("plan beat never reached the user" in f for f in result.failures)


def test_control_blocking_planner_call_is_caught(monkeypatch, tmp_path):
    from kiln.agents import planner

    async def run_inline(fn, *args, **kwargs):  # the pre-fix behavior: on the loop
        return fn(*args, **kwargs)

    monkeypatch.setattr(planner, "asyncio", types.SimpleNamespace(to_thread=run_inline))
    result = run_scenario(SCENARIOS["slow_planner"], tmp_path / "ws", timeout=150)
    assert result.failures
    assert any("event loop" in f or "companion started" in f or "run " in f for f in result.failures)


def test_control_single_handoff_attempt_is_caught(monkeypatch, tmp_path):
    from kiln.agents import loop

    monkeypatch.setattr(loop, "HANDOFF_ATTEMPTS", 1)
    result = run_scenario(SCENARIOS["empty_handoff"], tmp_path / "ws")
    assert any("rescued by salvage" in f for f in result.failures)


def test_control_path_only_blame_is_caught(monkeypatch, tmp_path):
    from kiln.graph import orchestrator

    def path_only(nodeid, path):  # the pre-fix matcher
        return path.rsplit("/", 1)[-1] in nodeid or path in nodeid

    monkeypatch.setattr(orchestrator, "_case_in_file", path_only)
    result = run_scenario(SCENARIOS["fix_loop"], tmp_path / "ws")
    assert any("fix cap" in f or "not green" in f or "run failed" in f for f in result.failures)
