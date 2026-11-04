"""Contract-first collaboration (docs/contract-collaboration-plan.md).

A precise contract both developers build from, generated shared types,
questions with answers, reviewed contract changes, and a conformance check
when a developer submits.
"""

import asyncio
import json

import pytest
from pydantic import ValidationError

from kiln.agents import planner
from kiln.agents import worker as worker_module
from kiln.config import Settings
from kiln.contract import (
    apply_change,
    change_conflicts,
    conformance_gaps,
    contract_digest,
    render_types,
    route_type_name,
    types_targets,
)
from kiln.gateway.client import Completion, ModelToolCall
from kiln.graph import orchestrator
from kiln.graph.orchestrator import Orchestrator
from kiln.schemas.suite import TestSuite
from kiln.schemas.tasks import Contract, ContractRoute, FieldSpec, Task, TaskGraph
from tests.test_orchestrator import SPEC, Collector, FakeGateway, FakeRegistry, _work
from tests.test_two_developers import SPLIT, _agentic, _t


def F(name, type_="string", required=True):
    return FieldSpec(name=name, type=type_, required=required)


SIGNUP = ContractRoute(
    method="POST", path="/api/signup", status=201,
    request_fields=[F("email"), F("password"), F("name", required=False)],
    response_fields=[F("token"), F("user_id", "integer")],
    errors=[400, 409],
)
NOTE = ContractRoute(method="GET", path="/api/notes/{id}", response_fields=[F("id"), F("title")])
CONTRACT = Contract(routes=[SIGNUP, NOTE], error_shape=[F("error")])


# ── 1. precise contract ──────────────────────────────────────────────────────


def test_vague_api_routes_are_named():
    assert ContractRoute(method="POST", path="/api/x").vagueness() == [
        "POST /api/x has no request_fields",
        "POST /api/x has no response_fields (use status 204 for no body)",
    ]
    assert ContractRoute(method="DELETE", path="/api/x/{id}", status=204).vagueness() == []
    assert ContractRoute(method="GET", path="/app", kind="page").vagueness() == []
    dup = ContractRoute(method="GET", path="/api/y", response_fields=[F("a"), F("a")])
    assert "repeats response field(s) a" in dup.vagueness()[0]


def test_precision_is_enforced_only_on_fresh_planner_output():
    graph = {"spec_title": "N", "tasks": [_t("a", ["a.py"])],
             "contract": {"routes": [{"method": "POST", "path": "/api/x"}]}}
    TaskGraph.model_validate(graph)  # old plans and tests stay valid
    with pytest.raises(ValidationError, match="exact fields"):
        TaskGraph.model_validate(graph, context={"precise_contract": True})


def test_a_planner_that_stays_vague_is_retried_then_accepted():
    vague = json.dumps({"spec_title": "N", "tasks": [_t("a", ["a.py"])],
                        "contract": {"routes": [{"method": "POST", "path": "/api/x"}]}})

    class GW:
        calls = []

        def complete(self, role, messages, **kwargs):
            GW.calls.append(messages)
            return Completion(content=vague, model="m", prompt_tokens=1, completion_tokens=1)

    graph = planner.run(GW(), SPEC)
    assert graph.contract.routes[0].path == "/api/x"
    assert "request_fields" in GW.calls[1][1]["content"]  # the retry named the problem


def test_the_contract_doc_shows_exact_fields():
    graph = TaskGraph.model_validate({"spec_title": "N", "tasks": [_t("a", ["a.py"])],
                                      "contract": CONTRACT.model_dump()})
    doc = planner.render_plan_markdown(SPEC, graph)
    assert "request: {email: string, password: string, name?: string}" in doc
    assert "201: {token: string, user_id: integer}" in doc
    assert "errors: 400, 409" in doc and "## Error body" in doc


# ── 2. generated shared types ────────────────────────────────────────────────


def test_type_names_are_stable_and_unique():
    assert route_type_name(NOTE) == "GetApiNotesById"
    assert route_type_name(ContractRoute(method="get", path="/api/users/:userId")) == "GetApiUsersByUserId"
    twin = Contract(routes=[NOTE, NOTE.model_copy()])
    assert "GetApiNotesById2Response" in render_types(twin, "ts")


def test_typescript_types_carry_exact_fields():
    ts = render_types(CONTRACT, "ts")
    assert "export interface PostApiSignupRequest {" in ts
    assert "  name?: string;" in ts and "  user_id: number;" in ts
    assert "export interface ApiError {" in ts
    assert 'postApiSignup: { method: "POST", path: "/api/signup", status: 201' in ts
    odd = Contract(routes=[ContractRoute(method="GET", path="/api/s", response_fields=[F("scene", "Scene")])])
    assert "scene: unknown; // contract type: Scene" in render_types(odd, "ts")


def test_python_types_are_importable():
    namespace: dict = {}
    exec(compile(render_types(CONTRACT, "py"), "contract.py", "exec"), namespace)
    assert namespace["POST_API_SIGNUP_REQUEST_REQUIRED"] == ("email", "password")
    assert set(namespace["PostApiSignupRequest"].__annotations__) == {"email", "password", "name"}


def test_javascript_types_have_no_runtime_code():
    js = render_types(CONTRACT, "js")
    assert "@typedef {{ email: string, password: string, name?: string }} PostApiSignupRequest" in js
    assert all(line.startswith(("//", "/**")) for line in js.splitlines() if line.strip())


def test_types_go_where_each_toolchain_already_looks():
    assert types_targets(SPLIT, CONTRACT) == [("backend/src/contract.js", "js"),
                                              ("frontend/src/contract.ts", "ts")]
    from tests.test_two_developers import _spec

    next_app = _spec([{"path": ".", "role": "fullstack", "framework": "nextjs"}])
    assert types_targets(next_app, CONTRACT) == [("lib/contract.ts", "ts")]
    assert types_targets(SPLIT, Contract(routes=[ContractRoute(method="GET", path="/", kind="page")])) == []


def test_the_reminder_digest_lists_fields():
    digest = contract_digest(CONTRACT)
    assert "POST /api/signup {email: string, password: string, name?: string} -> 201 {token: string" in digest


# ── 5. conformance ───────────────────────────────────────────────────────────


def test_a_backend_route_missing_a_response_field_is_caught():
    files = {"backend/src/routes/auth.js": (
        "router.post('/api/signup', async (req, res) => {\n"
        "  const { email, password, name } = req.body;\n"
        "  res.status(201).json({ token });\n});\n"
    )}
    gaps = conformance_gaps(files, CONTRACT)
    assert gaps == ["POST /api/signup (backend/src/routes/auth.js) never mentions user_id"]
    files["backend/src/routes/auth.js"] += "// user_id is returned too: user_id"
    assert conformance_gaps(files, CONTRACT) == []


def test_a_form_missing_a_required_field_is_caught_but_optional_ones_are_not():
    form = {"frontend/src/SignupForm.tsx": (
        "await fetch('/api/signup', { method: 'POST', body: JSON.stringify({ email }) })"
    )}
    gaps = conformance_gaps(form, CONTRACT)
    assert gaps == ["POST /api/signup is called from frontend/src/SignupForm.tsx without "
                    "the required field(s) password"]


def test_file_based_routes_and_path_params_are_recognised():
    files = {"app/api/notes/[id]/route.ts": "export async function GET() { return Response.json({ id }) }"}
    assert conformance_gaps(files, CONTRACT) == [
        "GET /api/notes/{id} (app/api/notes/[id]/route.ts) never mentions title"
    ]
    assert conformance_gaps({"lib/db.ts": "export const db = 1"}, CONTRACT) == []


# ── 4. changes ───────────────────────────────────────────────────────────────


def test_changes_are_described_and_new_routes_added():
    contract, old, new, summary = apply_change(
        CONTRACT, "POST", "/api/signup",
        request_fields=None, response_fields=[F("token"), F("user_id", "integer"), F("expires_at")],
        status=None,
    )
    assert old is SIGNUP and new.request_fields == SIGNUP.request_fields
    assert summary == "POST /api/signup: response adds expires_at: string"
    _, old, _, summary = apply_change(CONTRACT, "GET", "/api/tags", request_fields=None,
                                      response_fields=[F("tags", "string[]")], status=None)
    assert old is None and summary.startswith("new route GET /api/tags")


def test_removing_what_the_tests_use_is_a_conflict():
    _, old, new, _ = apply_change(CONTRACT, "POST", "/api/signup", request_fields=None,
                                  response_fields=[F("token")], status=None)
    assert change_conflicts(old, new, "expect(body.user_id).toBe(1)") == [
        "it removes response field 'user_id', which the tests use"
    ]
    assert change_conflicts(old, new, "expect(body.token)") == []
    _, old, new, _ = apply_change(CONTRACT, "POST", "/api/signup", request_fields=None,
                                  response_fields=None, status=200)
    assert "changes the status" in change_conflicts(old, new, "post('/api/signup')")[0]


# ── orchestration: artifacts, proposals, questions, the submit check ─────────


def _graph_with(contract=CONTRACT, *tasks):
    tasks = tasks or (_t("api", ["backend/src/routes.js"], root="backend"),
                      _t("ui", ["frontend/src/Form.tsx"], root="frontend"))
    return TaskGraph.model_validate({"spec_title": "N", "tasks": list(tasks),
                                     "contract": contract.model_dump()})


def _orch_with(graph, suite_text=""):
    orch = Orchestrator(FakeGateway(), FakeRegistry([]), Collector().send, 1, _agentic())
    orch._spec = SPLIT
    orch._graph = graph
    orch._suite = TestSuite.model_validate({"spec_title": "N", "files": [
        {"path": "tests/a.test.ts", "content": suite_text or "x", "covers": ["c1"]}]})
    return orch


def _written(orch):
    return {inp["path"]: inp["content"] for tool, inp in orch.registry.calls if tool == "write_file"}


def test_artifacts_are_written_and_task_owned_paths_skipped():
    graph = _graph_with(CONTRACT, _t("ui", ["frontend/src/contract.ts"], root="frontend"))
    orch = _orch_with(graph)
    asyncio.run(orch._write_contract_artifacts("r", SPLIT, graph))
    written = _written(orch)
    assert json.loads(written["docs/api-contract.json"])["routes"][0]["path"] == "/api/signup"
    assert "backend/src/contract.js" in written
    assert "frontend/src/contract.ts" not in written  # a task owns it
    assert any("a task owns that path" in note for note in orch._run_notes)


def _run_lanes(orch, graph, fake, monkeypatch):
    monkeypatch.setattr(orchestrator.worker, "run_agentic", fake)
    asyncio.run(orch._run_tasks("r", list(graph.tasks), SPLIT, orch._suite, {}, graph, "",
                                state=None, smoke=False))


def test_an_accepted_change_regenerates_types_and_tells_the_teammate(monkeypatch):
    """The form scenario: the backend adds a field, the frontend is told
    mid-task, and the shared types now carry it."""
    graph = _graph_with()
    orch = _orch_with(graph, suite_text="expect(body.token)")
    asyncio.run(orch._write_contract_artifacts("r", SPLIT, graph))
    seen = {}

    async def fake(gw, task, spec, call_tool, **kwargs):
        seen.setdefault("brief", kwargs["team_context"])
        if task.id == "api":
            await asyncio.sleep(0.01)
            from kiln.tools.schemas import ContractField, ProposeContractChange

            payload = ProposeContractChange(
                method="POST", path="/api/signup", request_fields=None,
                response_fields=[ContractField(name="token", type="string"),
                                 ContractField(name="user_id", type="integer"),
                                 ContractField(name="display_name", type="string")],
                status=None, reason="the form greets the new user by name",
            )
            seen["reply"] = await kwargs["on_propose"](payload)
        else:
            await asyncio.sleep(0.1)
            seen["inbox"] = kwargs["inbox"]()
        return _work(task.id, task.owned_paths[0])

    _run_lanes(orch, graph, fake, monkeypatch)
    assert seen["reply"].startswith("accepted: POST /api/signup: response adds display_name: string")
    assert any("CONTRACT CHANGED" in note and "display_name" in note for note in seen["inbox"])
    assert "display_name: string;" in _written(orch)["frontend/src/contract.ts"]
    assert "display_name" in _written(orch)["docs/contract.md"]
    assert graph.contract.routes[0].response_fields[-1].name == "display_name"
    assert "<contract_types>" in seen["brief"] and "frontend/src/contract.ts" in seen["brief"]
    assert any("contract change by Dev A" in note for note in orch._run_notes)


def test_a_change_that_breaks_the_tests_is_refused(monkeypatch):
    from kiln.tools.schemas import ContractField, ProposeContractChange

    graph = _graph_with()
    orch = _orch_with(graph, suite_text="expect(body.user_id).toBeDefined()")
    orch._board = None
    orch._team(graph)
    payload = ProposeContractChange(method="POST", path="/api/signup", request_fields=None,
                                    response_fields=[ContractField(name="token", type="string")],
                                    status=None, reason="simpler")
    reply = asyncio.run(orch._propose_contract_change("r", "A", payload))
    assert reply.startswith("refused: it removes response field 'user_id'")
    assert graph.contract.routes[0].response_fields[1].name == "user_id"  # unchanged
    vague = ProposeContractChange(method="POST", path="/api/new", request_fields=None,
                                  response_fields=None, status=None, reason="x")
    assert "has no request_fields" in asyncio.run(orch._propose_contract_change("r", "A", vague))


def test_a_question_is_answered_while_the_asker_keeps_working(monkeypatch):
    graph = _graph_with()
    orch = _orch_with(graph)
    seen = {}

    async def fake(gw, task, spec, call_tool, **kwargs):
        if task.id == "ui":
            seen["asked"] = await kwargs["on_ask"]("does /api/signup trim the email?", False)
            await asyncio.sleep(0.15)
            seen["inbox"] = kwargs["inbox"]()
        else:
            await asyncio.sleep(0.05)
            seen["api_inbox"] = kwargs["inbox"]()
            seen["answered"] = await kwargs["on_notify"]("yes, and lowercases it", "Q1")
        return _work(task.id, task.owned_paths[0])

    _run_lanes(orch, graph, fake, monkeypatch)
    assert seen["asked"].startswith("asked Q1; keep working")
    assert any('reply_to="Q1"' in note for note in seen["api_inbox"])
    assert seen["answered"] == "answered Q1"
    assert any("answer to Q1: yes, and lowercases it" in note for note in seen["inbox"])


def test_wait_true_pauses_until_the_answer(monkeypatch):
    graph = _graph_with()
    orch = _orch_with(graph)
    seen = {}

    async def fake(gw, task, spec, call_tool, **kwargs):
        if task.id == "ui":
            await asyncio.sleep(0.01)
            seen["reply"] = await kwargs["on_ask"]("which field holds the user id?", True)
        else:
            await asyncio.sleep(0.05)
            await kwargs["on_notify"]("user_id, an integer", "Q1")
            await asyncio.sleep(0.05)
        return _work(task.id, task.owned_paths[0])

    _run_lanes(orch, graph, fake, monkeypatch)
    assert seen["reply"] == "answer to Q1 from Dev A (backend): user_id, an integer"


def test_a_waiting_asker_is_woken_when_the_teammate_finishes_without_answering(monkeypatch):
    graph = _graph_with()
    orch = _orch_with(graph)
    seen = {}

    async def fake(gw, task, spec, call_tool, **kwargs):
        if task.id == "ui":
            await asyncio.sleep(0.01)
            seen["reply"] = await kwargs["on_ask"]("anything?", True)
        else:
            await asyncio.sleep(0.05)
        return _work(task.id, task.owned_paths[0])

    _run_lanes(orch, graph, fake, monkeypatch)
    assert seen["reply"].startswith("no answer to Q1 yet")


def test_waiting_on_an_idle_or_waiting_teammate_is_refused():
    graph = _graph_with()
    orch = _orch_with(graph)
    board = orch._team(graph)
    reply = asyncio.run(orch._ask_peer("B", "q?", True))
    assert "between tasks" in reply  # Dev A is idle: nobody can answer now
    board.lanes["A"].current = "api"
    board.lanes["B"].current = "ui"
    board.ask("A", "B", "q from A")
    board.awaiting["A"] = "Q2"
    reply = asyncio.run(orch._ask_peer("B", "q from B", True))
    assert "cannot both wait" in reply


def test_the_submit_check_warns_once_then_records_the_drift(monkeypatch):
    task = Task(id="api", title="API", description="d", owned_paths=["backend/src/routes.js"])
    submit = json.dumps({"task_id": "api", "summary": "s", "files_written": ["backend/src/routes.js"],
                         "what_works": "w", "known_gaps": []})
    turns = [Completion(content="", model="m", prompt_tokens=1, completion_tokens=1,
                        tool_calls=(ModelToolCall(f"c{i}", "submit_result", submit),)) for i in range(2)]

    class GW:
        settings = Settings(_env_file=None, enable_explore_subagent=False, enable_todo_tool=False,
                            post_edit_diagnostics=False)
        seen = []

        def complete(self, role, messages, **kwargs):
            GW.seen.append(messages)
            return turns.pop(0)

    async def call_tool(name, args):
        return "router.post('/api/signup', (req, res) => res.status(201).json({ token }))"

    async def pre_submit(paths):
        return conformance_gaps({p: await call_tool("read_file", {"path": p}) for p in paths}, CONTRACT)[0]

    result = asyncio.run(worker_module.run_agentic(GW(), task, SPEC, call_tool, pre_submit=pre_submit))
    rejected = [m["content"] for m in GW.seen[1] if m["role"] == "tool"]
    assert any("contract check:" in text and "never mentions" in text for text in rejected)
    assert any(gap.startswith("contract check (static, unconfirmed)") for gap in result.known_gaps)


def test_page_routes_are_smoked_even_under_api():
    graph = _graph_with(Contract(routes=[ContractRoute(method="GET", path="/api/docs", kind="page"),
                                         ContractRoute(method="GET", path="/users/:id", kind="page")]))
    assert Orchestrator._smoke_paths(graph) == ["/", "/api/docs"]
