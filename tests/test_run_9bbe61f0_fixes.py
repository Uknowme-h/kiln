"""Regression tests for the run-9bbe61f0 postmortem (docs/postmortem-run-9bbe61f0-plan.md).

The run died because create-next-app could not scaffold a folder named
`Notescalidraw`, the run carried on without a skeleton, nothing installed the
suite's runner, compaction orphaned a tool message (HTTP 400), grep's include
missed bare file names, and the crash was blamed on the reporter.
"""

import asyncio
import json
from pathlib import Path

import pytest

from kiln.agents.loop import _safe_tail
from kiln.agents.roles import Role
from kiln.companion.executor import ExecutionError, Executor
from kiln.companion.process import CommandResult
from kiln.companion.protocol import AgentScope
from kiln.companion.registry import ToolDenied
from kiln.config import Settings
from kiln.gateway.budget import BudgetGuard
from kiln.gateway.client import Completion, Gateway, _provider_detail
from kiln.gateway.errors import GatewayError
from kiln.gateway.usage import UsageLog
from kiln.graph import orchestrator
from kiln.graph.orchestrator import Orchestrator
from kiln.schemas.spec import Spec
from kiln.stacks.profiles import VITEST_SPEC, plan_node_suite_manifest
from kiln.stacks.scaffold_recipes import npm_package_name
from tests.test_executor import AlwaysAllow
from tests.test_orchestrator import (
    TEST_SETTINGS,
    Collector,
    FakeGateway,
    FakeRegistry,
    _agents,
    _report,
)

ORCH_SCOPE = AgentScope(id="orchestrator", label="Kiln", role="planner", owned_paths=[])


def _node_spec(roots) -> Spec:
    return Spec.model_validate(
        {
            "title": "Notes",
            "summary": "s",
            "stack": "Next.js",
            "stack_id": "node",
            "frameworks": sorted({r["framework"] for r in roots}),
            "roots": roots,
            "user_stories": [{"id": "s1", "as_a": "u", "i_want": "w", "so_that": "t"}],
            "acceptance_criteria": [
                {"id": "c1", "story_id": "s1", "given": "g", "when": "w", "then": "t",
                 "oracle": "tests/api.test.ts — GET /api/health returns 200"}
            ],
            "file_plan": [{"path": "app/page.tsx", "purpose": "landing"}],
        }
    )


# ── 1. staged scaffold: npm-valid name, empty target, no overwrite ───────────


@pytest.mark.parametrize(
    ("folder", "expected"),
    [
        ("Notescalidraw", "notescalidraw"),
        ("My Cool App!", "my-cool-app"),
        ("frontend", "frontend"),
        ("__Weird__..", "weird"),
        ("!!!", "app"),
    ],
)
def test_npm_package_name_is_always_valid(folder, expected):
    assert npm_package_name(folder) == expected


def _fake_create_cli(ran):
    """Stands in for create-next-app: builds `<cwd>/<name>` like the real CLI."""

    def fake_run(argv, cwd, timeout=600, env=None):
        ran.append((list(argv), Path(cwd)))
        if "create-next-app@15" in argv:
            built = Path(cwd) / argv[3]
            assert not built.exists(), "the CLI must get a fresh, empty target"
            (built / "app").mkdir(parents=True)
            (built / "app" / "page.tsx").write_text("export default () => null\n")
            (built / "next.config.ts").write_text("export default {}\n")
            (built / "package.json").write_text(json.dumps({"name": argv[3]}))
            (built / "node_modules" / "next").mkdir(parents=True)
            (built / "docs").mkdir()
            (built / "docs" / "discovery.md").write_text("from the template\n")
        return CommandResult(0, "created")

    return fake_run


def test_nextjs_scaffolds_into_a_capitalised_non_empty_folder(tmp_path, monkeypatch):
    workspace = tmp_path / "Notescalidraw"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs" / "discovery.md").write_text("the user's decisions\n")
    (workspace / ".kiln").mkdir()
    ran = []
    monkeypatch.setattr("kiln.companion.executor.run_argv", _fake_create_cli(ran))
    executor = Executor(workspace, Settings(_env_file=None, sandbox="host"), prompt=AlwaysAllow())

    out = executor.execute(
        "scaffold_framework", {"framework": "nextjs", "root": ".", "options": {}}, ORCH_SCOPE
    )

    argv, cwd = ran[0]
    assert "notescalidraw" in argv and "." not in argv  # npm-valid name, not "."
    assert ".kiln" in cwd.parts and "scaffold" in cwd.parts  # staged, same volume
    assert (workspace / "next.config.ts").is_file()
    assert (workspace / "app" / "page.tsx").is_file()
    assert (workspace / "node_modules" / "next").is_dir()
    assert json.loads((workspace / "package.json").read_text())["name"] == "notescalidraw"
    # the user's file is kept, never overwritten — and the kept path is reported
    assert (workspace / "docs" / "discovery.md").read_text() == "the user's decisions\n"
    assert "kept existing (not overwritten): docs/discovery.md" in out
    assert not (workspace / ".kiln" / "scaffold").exists()  # staging cleaned up


def test_a_failing_create_cli_cleans_up_and_raises(tmp_path, monkeypatch):
    def failing(argv, cwd, timeout=600, env=None):
        return CommandResult(1, "npm error boom")

    monkeypatch.setattr("kiln.companion.executor.run_argv", failing)
    executor = Executor(tmp_path, Settings(_env_file=None, sandbox="host"), prompt=AlwaysAllow())
    with pytest.raises(ExecutionError, match="boom"):
        executor.execute(
            "scaffold_framework", {"framework": "nextjs", "root": ".", "options": {}}, ORCH_SCOPE
        )
    assert not (tmp_path / ".kiln" / "scaffold").exists()
    assert not (tmp_path / "next.config.ts").exists()


# ── 2. a failed scaffold ends the run ────────────────────────────────────────


class _ScaffoldRegistry(FakeRegistry):
    def __init__(self, error, **kwargs):
        super().__init__([_report(passed=1, green=True)] * 4, **kwargs)
        self.error = error

    async def call_tool(self, run_id, tool, tool_input, timeout=300, agent=None):
        if tool == "scaffold_framework":
            self.calls.append((tool, tool_input))
            raise self.error
        return await super().call_tool(run_id, tool, tool_input, timeout, agent)


def _run_with_scaffold_error(monkeypatch, error):
    _agents(monkeypatch)
    spec = _node_spec([{"path": ".", "role": "fullstack", "framework": "nextjs"}])

    async def fake_requirements(*_a, **_k):
        return spec

    monkeypatch.setattr(orchestrator.requirements, "run_async", fake_requirements)
    registry = _ScaffoldRegistry(error)
    events = Collector()
    orch = Orchestrator(FakeGateway(), registry, events.send, 1, TEST_SETTINGS)
    green = asyncio.run(orch.run("run-1", "notes app", []))
    return green, registry, events


def _skeleton_claims(registry):
    return [
        inp["owned_paths"] for tool, inp in registry.calls
        if tool == "claim_scope" and "package.json" in inp.get("owned_paths", [])
    ]


def test_a_failed_scaffold_falls_back_to_a_hand_built_skeleton(monkeypatch):
    """A failed official scaffold no longer leaves the team building on
    nothing, and no longer ends the run: the root's first task is leased the
    essential files and told to build the skeleton by hand."""
    green, registry, events = _run_with_scaffold_error(
        monkeypatch,
        RuntimeError('ExecutionError: Could not create a project called "Notescalidraw"'),
    )
    claims = _skeleton_claims(registry)
    assert claims and {"package.json", "next.config.ts", "tsconfig.json"} <= set(claims[0])
    notice = [e for e in events.events if e.kind == "message_sent" and "by hand" in e.summary]
    assert notice and "Notescalidraw" in notice[0].summary
    assert "run_tests" in [tool for tool, _ in registry.calls]  # the build went on
    assert green is True


def test_a_declined_scaffold_also_falls_back(monkeypatch):
    green, registry, events = _run_with_scaffold_error(monkeypatch, ToolDenied("user said no"))
    assert _skeleton_claims(registry)
    notice = [e for e in events.events if e.kind == "message_sent" and "by hand" in e.summary]
    assert "declined" in notice[0].summary


def test_the_skeleton_fallback_never_claims_a_file_another_task_owns(monkeypatch):
    from kiln.schemas.tasks import TaskGraph

    graph = TaskGraph.model_validate({
        "spec_title": "N",
        "tasks": [
            {"id": "s", "title": "S", "description": "d", "kind": "scaffold", "owned_paths": ["app/layout.tsx"]},
            {"id": "f", "title": "F", "description": "d", "owned_paths": ["package.json", "app/page.tsx"],
             "depends_on": ["s"]},
        ],
    })
    orch = Orchestrator(FakeGateway(), FakeRegistry([]), Collector().send, 1, TEST_SETTINGS)
    asyncio.run(orch._skeleton_fallback(graph, ".", "nextjs", "boom"))
    scaffold = graph.tasks[0]
    assert "package.json" not in scaffold.owned_paths  # f owns it
    assert "next.config.ts" in scaffold.owned_paths
    assert "nextjs" in scaffold.required_skills
    assert "SKELETON FALLBACK" in scaffold.description


def test_an_already_scaffolded_root_is_still_reused(monkeypatch):
    green, registry, _ = _run_with_scaffold_error(
        monkeypatch, RuntimeError("root '.' is already scaffolded (next.config.ts exists)")
    )
    assert green is True


# ── 3. the suite runner is guaranteed ────────────────────────────────────────


def test_a_missing_manifest_is_created_with_vitest(tmp_path):
    data, notes = plan_node_suite_manifest(tmp_path, "notes")
    assert data["name"] == "notes"
    assert data["devDependencies"]["vitest"] == VITEST_SPEC
    assert data["scripts"]["test"] == "vitest run"
    assert notes[0] == "create package.json"


def test_an_existing_manifest_gains_only_what_it_lacks(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "web", "scripts": {"dev": "next dev", "test": 'echo "Error: no test specified" && exit 1'},
        "dependencies": {"next": "15.0.0"},
    }))
    data, notes = plan_node_suite_manifest(tmp_path, "ignored")
    assert data["name"] == "web" and data["dependencies"] == {"next": "15.0.0"}
    assert data["scripts"] == {"dev": "next dev", "test": "vitest run"}
    assert len(notes) == 2

    (tmp_path / "package.json").write_text(json.dumps(data))
    assert plan_node_suite_manifest(tmp_path, "ignored")[1] == []  # idempotent


def test_a_custom_test_script_is_left_alone(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {"test": "vitest run --coverage"}, "devDependencies": {"vitest": "^2"},
    }))
    assert plan_node_suite_manifest(tmp_path, "x")[1] == []


def test_an_invalid_manifest_is_never_rewritten(tmp_path):
    (tmp_path / "package.json").write_text("{ not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        plan_node_suite_manifest(tmp_path, "x")


def test_install_deps_at_the_suite_root_adds_vitest_and_installs(tmp_path, monkeypatch):
    ran = []

    def fake_run(argv, cwd, timeout=600, env=None):
        ran.append(list(argv))
        return CommandResult(0, "added 1 package")

    monkeypatch.setattr("kiln.companion.executor.run_argv", fake_run)
    executor = Executor(tmp_path, Settings(_env_file=None, sandbox="host"), prompt=AlwaysAllow())
    out = executor.execute("install_deps", {"stack_id": "node", "root": "."}, ORCH_SCOPE)
    manifest = json.loads((tmp_path / "package.json").read_text())
    assert manifest["devDependencies"]["vitest"] == VITEST_SPEC
    assert ran == [["npm", "install"]]
    assert "add vitest" in out


def test_a_missing_runner_is_named_not_buried_in_npx_noise(tmp_path):
    executor = Executor(tmp_path, Settings(_env_file=None, sandbox="host"), prompt=AlwaysAllow())
    with pytest.raises(ExecutionError, match="vitest is not installed at the workspace root"):
        executor.execute("run_tests", {"stack_id": "node"}, ORCH_SCOPE)


def test_a_split_node_product_installs_the_runner_at_the_workspace_root(monkeypatch):
    spec = _node_spec([
        {"path": "frontend", "role": "web", "framework": "vite_react"},
        {"path": "backend", "role": "api", "framework": "express"},
    ])
    registry = FakeRegistry([])
    orch = Orchestrator(FakeGateway(), registry, Collector().send, 1, TEST_SETTINGS)
    orch.stack_id = "node"
    asyncio.run(orch._install_remaining_roots("run-1", spec))
    roots = [inp.get("root") for tool, inp in registry.calls if tool == "install_deps"]
    assert roots == ["frontend", "backend", "."]
    asyncio.run(orch._install_remaining_roots("run-1", spec))  # each root installs once
    assert len([1 for tool, _ in registry.calls if tool == "install_deps"]) == 3


def test_a_failed_suite_root_install_is_recorded_not_fatal(monkeypatch):
    """No single failure ends the build: the suite's self-heal reinstalls once
    and the final run gives the honest verdict."""
    spec = _node_spec([{"path": ".", "role": "fullstack", "framework": "nextjs"}])

    class FailingInstall(FakeRegistry):
        async def call_tool(self, run_id, tool, tool_input, timeout=300, agent=None):
            if tool == "install_deps":
                raise RuntimeError("npm ERR! network")
            return await super().call_tool(run_id, tool, tool_input, timeout, agent)

    orch = Orchestrator(FakeGateway(), FailingInstall([]), Collector().send, 1, TEST_SETTINGS)
    orch.stack_id = "node"
    asyncio.run(orch._install_remaining_roots("run-1", spec))
    assert orch._root_state == {".": "failed"}
    assert any("npm ERR! network" in note for note in orch._run_notes)


# ── 4. compaction never orphans a tool result; the gateway checks pairing ────


def test_the_compaction_tail_never_opens_on_a_tool_result():
    history = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}, {"id": "b"}, {"id": "c"}]},
        {"role": "tool", "tool_call_id": "a", "content": "x"},
        {"role": "tool", "tool_call_id": "b", "content": "x"},
        {"role": "tool", "tool_call_id": "c", "content": "x"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "d"}]},
        {"role": "tool", "tool_call_id": "d", "content": "x"},
    ]
    tail = _safe_tail(history, 4)  # a plain slice would open on tool "b"
    assert tail[0]["role"] == "assistant"
    assert [m.get("tool_call_id") for m in tail] == [None, "d"]


def _gateway():
    class Transport:
        def complete(self, **kwargs):
            return Completion(content="ok", model="m", prompt_tokens=1, completion_tokens=1)

    settings = Settings(_env_file=None, model_api_key="k")
    return Gateway(settings, BudgetGuard(10_000), UsageLog(), transport=Transport())


def test_the_gateway_refuses_an_orphaned_tool_message_locally():
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "summary after compaction"},
        {"role": "tool", "tool_call_id": "call_5ac1", "content": "(no matches)"},
    ]
    with pytest.raises(GatewayError, match="orphaned tool message"):
        _gateway().complete(Role.WORKER, messages)


def test_the_gateway_accepts_paired_tool_messages():
    messages = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "grep", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "a"},
        {"role": "tool", "tool_call_id": "c2", "content": "b"},
    ]
    assert _gateway().complete(Role.WORKER, messages).content == "ok"


# ── 5. grep include matches bare file names ──────────────────────────────────


def test_grep_include_matches_a_bare_file_name(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.tsx").write_text("<h1>Inkwell</h1>\n")
    (tmp_path / "app" / "other.tsx").write_text("<h1>Other</h1>\n")
    executor = Executor(tmp_path, Settings(_env_file=None, sandbox="host"), prompt=AlwaysAllow())
    out = executor.execute("grep", {"pattern": "h1", "path": "app", "include": "page.tsx"})
    assert "app/page.tsx:1" in out and "other.tsx" not in out
    out = executor.execute("grep", {"pattern": "h1", "include": "app/*.tsx"})
    assert "app/page.tsx" in out and "app/other.tsx" in out


# ── 6. crashes are reported where they happened; 400s say why ────────────────


def test_a_crash_is_reported_on_the_failing_stage_not_the_reporter(monkeypatch):
    async def boom(*_a, **_k):
        raise ValueError("model refused the spec")

    monkeypatch.setattr(orchestrator.requirements, "run_async", boom)
    events = Collector()
    orch = Orchestrator(FakeGateway(), FakeRegistry([]), events.send, 1, TEST_SETTINGS)
    assert asyncio.run(orch.run("run-1", "notes", [])) is False
    stuck = [e for e in events.events if e.kind == "agent_status" and e.status == "stuck"]
    assert [e.agent.id for e in stuck] == ["requirements"]
    note = [e for e in events.events if e.kind == "message_sent" and "run failed" in e.summary]
    assert note[0].summary.startswith("run failed during requirements: model refused the spec")


def test_provider_error_text_is_surfaced_and_clipped():
    class Fake(Exception):
        body = {"error": {"message": "messages.3: tool result has no matching tool_call", "type": "bad"}}

    assert _provider_detail(Fake()) == ": messages.3: tool result has no matching tool_call"

    class Long(Exception):
        body = {"error": {"message": "x" * 2_000}}

    assert len(_provider_detail(Long())) < 520

    class Empty(Exception):
        body = None
        message = ""

    assert _provider_detail(Empty()) == ""
