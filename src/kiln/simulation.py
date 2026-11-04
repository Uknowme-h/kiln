"""Deterministic end-to-end simulation of a full Kiln run — zero model tokens.

Everything is real except the model: the uvicorn server and its event loop,
the companion websocket, the orchestrator and every agent loop, the companion
Executor (permissions, leases, read ledger, scope-state persistence), the
scaffold recipe code, real file writes, and real pytest runs of the generated
suite. The model is scripted per role, with targeted fault injection.

Why this exists: every bug the expensive live evals found was a HARNESS bug
that a particular model output triggered — a blocking call on the event loop,
a single handoff attempt, an offloaded read_file, a lease lost on reconnect,
a scaffold owning its whole root, a fatal "no changes needed". The live model
was only needed to *discover* the triggering output once. Each scenario below
replays one such trigger in seconds, so none is ever re-discovered at live-run
prices. Live evals stay for what only a real model can tell you: output
quality, prompt effectiveness, token efficiency.

Deliberately simulated (not exercised here): Playwright browser gates, the
host HTTP runtime smoke, ``npm create`` (an offline vite template stands in),
and dependency installs (the host interpreter already has the stack).

    from kiln.simulation import SCENARIOS, run_scenario
    result = run_scenario(SCENARIOS["companion_drop"], tmp_path)
    assert not result.failures, result.failures
"""

from __future__ import annotations

import asyncio
import copy
import functools
import json
import logging
import socket
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from kiln.agents.roles import Role
from kiln.companion.executor import Executor
from kiln.companion.prompt import ApprovalPrompt, Reply
from kiln.gateway.client import Completion, ModelToolCall
from kiln.stacks.scaffold_recipes import Recipe

# ── the product the simulated team builds ────────────────────────────────────

IDEA = "A notes app: a FastAPI backend in backend/ and a Vite + React frontend in frontend/ that lists notes."

SPEC = {
    "title": "Sim Notes",
    "summary": "List notes through an API and show them on a page.",
    "stack": "FastAPI + Vite/React",
    "stack_id": "python",
    "frameworks": ["fastapi", "vite_react"],
    "roots": [
        {"path": "backend", "role": "api", "framework": "fastapi"},
        {"path": "frontend", "role": "web", "framework": "vite_react"},
    ],
    "user_stories": [
        {"id": "s1", "as_a": "user", "i_want": "to list my notes", "so_that": "I find them"},
        {"id": "s2", "as_a": "user", "i_want": "a notes page", "so_that": "I see them"},
    ],
    "acceptance_criteria": [
        {"id": "c1", "story_id": "s1", "given": "no notes", "when": "GET /api/notes",
         "then": "200 and []", "oracle": "tests/test_api.py — GET /api/notes returns 200 []"},
        {"id": "c2", "story_id": "s1", "given": "the API is up", "when": "GET /health",
         "then": "200 ok", "oracle": "tests/test_api.py — GET /health returns {status: ok}"},
        {"id": "c3", "story_id": "s2", "given": "the web app", "when": "it renders",
         "then": "NotesPage is mounted", "oracle": "tests/test_web.py — App.tsx mounts NotesPage"},
    ],
    "file_plan": [
        {"path": "backend/app/main.py", "purpose": "API entry"},
        {"path": "backend/app/routers/notes.py", "purpose": "notes router"},
        {"path": "frontend/src/App.tsx", "purpose": "web shell"},
        {"path": "frontend/src/pages/NotesPage.tsx", "purpose": "notes page"},
    ],
    "assumptions": [],
}


def _task(tid, kind, root, owned, depends_on=(), covers=()):
    return {"id": tid, "title": tid, "description": f"{tid} (simulated)", "kind": kind,
            "root": root, "owned_paths": list(owned), "depends_on": list(depends_on),
            "covers": list(covers)}


GRAPH = {
    "spec_title": "Sim Notes",
    "tasks": [
        _task("S-BE", "scaffold", "backend", ["backend/app/main.py"], covers=["c2"]),
        _task("S-FE", "scaffold", "frontend", ["frontend/src/App.tsx"]),
        _task("F-NOTES", "feature", "backend", ["backend/app/routers/notes.py"], ["S-BE"], ["c1"]),
        _task("F-UI", "feature", "frontend", ["frontend/src/pages/NotesPage.tsx"], ["S-FE"], ["c3"]),
    ],
}

SUITE = {
    "spec_title": "Sim Notes",
    "files": [
        {"path": "tests/test_api.py", "covers": ["c1", "c2"], "content": (
            "import pathlib\nimport sys\n\n"
            "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'backend'))\n\n"
            "from fastapi.testclient import TestClient\n\nfrom app.main import app\n\n"
            "client = TestClient(app)\n\n\n"
            "def test_health():\n    r = client.get('/health')\n"
            "    assert r.status_code == 200 and r.json() == {'status': 'ok'}\n\n\n"
            "def test_list_notes_starts_empty():\n    r = client.get('/api/notes')\n"
            "    assert r.status_code == 200 and r.json() == []\n")},
        {"path": "tests/test_web.py", "covers": ["c3"], "content": (
            "from pathlib import Path\n\n"
            "SRC = Path(__file__).resolve().parents[1] / 'frontend' / 'src'\n\n\n"
            "def test_app_mounts_the_notes_page():\n"
            "    assert 'NotesPage' in (SRC / 'App.tsx').read_text(encoding='utf-8')\n\n\n"
            "def test_notes_page_exists():\n"
            "    page = (SRC / 'pages' / 'NotesPage.tsx').read_text(encoding='utf-8')\n"
            "    assert 'export default function NotesPage' in page\n")},
    ],
}

FILES = {
    "backend/app/main.py": (
        '"""Application entry point. Routers are wired here, nowhere else."""\n\n'
        "from fastapi import FastAPI\n\nfrom app.routers import health, notes\n\n"
        "app = FastAPI(title='sim-notes')\napp.include_router(health.router)\n"
        "app.include_router(notes.router, prefix='/api')\n"),
    "backend/app/routers/notes.py": (
        "from fastapi import APIRouter\n\nrouter = APIRouter(tags=['notes'])\n"
        "_NOTES: list[dict] = []\n\n\n@router.get('/notes')\n"
        "def list_notes() -> list[dict]:\n    return _NOTES\n"),
    "frontend/src/App.tsx": (
        "import NotesPage from './pages/NotesPage'\n\n"
        "export default function App() {\n  return <NotesPage />\n}\n"),
    "frontend/src/pages/NotesPage.tsx": (
        "export default function NotesPage() {\n  return <ul aria-label=\"notes\" />\n}\n"),
}

# a bug the suite catches (wrong envelope) — the fixer's job in fix_loop
BUGGY = {"backend/app/routers/notes.py": FILES["backend/app/routers/notes.py"].replace(
    "return _NOTES", "return {'notes': _NOTES}")}

# offline stand-in for `npm create vite` (same manifest, so dirty-root logic holds)
OFFLINE_VITE = Recipe(
    id="vite_react", kind="template", manifest="vite.config.ts",
    template_files=(
        ("package.json", '{"name": "web", "private": true, "type": "module"}\n'),
        ("vite.config.ts", "export default {}\n"),
        ("index.html", '<div id="root"></div><script type="module" src="/src/main.tsx"></script>\n'),
        ("src/main.tsx", "import App from './App.tsx'\n"),
        ("src/App.tsx", "export default function App() {\n  return null\n}\n"),
    ),
    note="offline vite template (simulation)",
)

ALLOWED_TOP_LEVEL = {
    "backend", "frontend", "tests", "docs", ".kiln", "README.md", "AGENTS.md",
    "kiln-results.xml", ".pytest_cache", "__pycache__",
}


# ── scenarios ────────────────────────────────────────────────────────────────


@dataclass
class Scenario:
    name: str
    why: str  # the live failure this replays
    graphs: list[dict] = field(default_factory=lambda: [copy.deepcopy(GRAPH)])
    worker_policy: dict[str, str] = field(default_factory=dict)  # task id -> policy
    fixes: dict[str, str] = field(default_factory=dict)
    planner_delay: float = 0.0  # seconds the task-graph call blocks
    keepalive: float | None = None  # shorten the companion's ping cycle
    companion_restarts: int = 0  # how often the supervisor may respawn it
    drop_before: tuple[str, int] | None = None  # (task id, turn) → drop the socket
    expect_green: bool = True
    checks: list[Callable[[SimResult], str | None]] = field(default_factory=list)


def _graph(**overrides) -> dict:
    graph = copy.deepcopy(GRAPH)
    for task in graph["tasks"]:
        task.update(overrides.get(task["id"], {}))
    return graph


def _check_companion_starts(expected: int) -> Callable[[SimResult], str | None]:
    def check(result: SimResult) -> str | None:
        if result.companion_starts != expected:
            return f"companion started {result.companion_starts}x, expected {expected}"
        return None
    return check


def _check_planner_retry_said(text: str) -> Callable[[SimResult], str | None]:
    def check(result: SimResult) -> str | None:
        if not any(text in msg for msg in result.planner_retry_prompts):
            return f"the planner's retry prompt never contained {text!r}"
        return None
    return check


def _check_rejected_submit(result: SimResult) -> str | None:
    if not any(r for r in result.transcript if r.get("kind") == "tool_result"
               and r["payload"].get("name") == "submit_result" and r["payload"].get("error")):
        return "expected a logged submit_result rejection"
    return None


def _salvaged(result: SimResult) -> bool:
    return any(r.get("kind") == "note" and r["payload"].get("text") == "salvaged partial result"
               for r in result.transcript)


def _check_no_salvage(result: SimResult) -> str | None:
    """The handoff retry, not the salvage fallback, must recover this one."""
    return "the run was rescued by salvage, not by the handoff retry" if _salvaged(result) else None


def _check_salvaged(result: SimResult) -> str | None:
    return None if _salvaged(result) else "expected a salvaged partial result"


def _check_fixer_ran(result: SimResult) -> str | None:
    return None if result.model_calls.get("fixer") else "the fixer never ran"


def _check_claims_replayed(result: SimResult) -> str | None:
    """The server-side replay is the authority; the workspace cache must not be
    what saves the run (it masked a replay deadlock until this check existed)."""
    if not any(n >= 1 for n in result.claims_replayed):
        return f"no active claim was replayed after the reconnect (replays: {result.claims_replayed})"
    return None


def _check_under(seconds: float) -> Callable[[SimResult], str | None]:
    def check(result: SimResult) -> str | None:
        return None if result.seconds < seconds else f"took {result.seconds}s (budget {seconds}s)"
    return check


_whole_root = _graph(**{"S-BE": {"owned_paths": ["backend"]}, "S-FE": {"owned_paths": ["frontend"]}})
_root_relative = _graph(**{
    "F-NOTES": {"owned_paths": ["app/routers/notes.py"]},
    "F-UI": {"owned_paths": ["src/pages/NotesPage.tsx"]},
})
_with_health = copy.deepcopy(GRAPH)
_with_health["tasks"].append(
    _task("F-HEALTH", "feature", "backend", ["backend/app/routers/health.py"], ["S-BE"], ["c2"]))

SCENARIOS: dict[str, Scenario] = {s.name: s for s in [
    Scenario("happy", "baseline: the full multi-root pipeline goes green"),
    Scenario("root_relative_paths",
             "run-7f74e69c: root-relative owned paths wrote stray app/ and src/ trees",
             graphs=[_root_relative]),
    Scenario("whole_root_scaffold",
             "run-7f74e69c: whole-root scaffold leases swallowed every feature file",
             graphs=[_whole_root, copy.deepcopy(GRAPH)],
             checks=[_check_planner_retry_said("seam files")]),
    Scenario("empty_handoff",
             "run-afe452e5: a finished worker sent submit_result {} at the cap and the run died",
             worker_policy={"F-NOTES": "burn_then_empty_handoff"},
             checks=[_check_rejected_submit, _check_no_salvage]),
    Scenario("salvage",
             "a worker that wrote its files but never produces a valid handoff must not kill the run",
             worker_policy={"F-NOTES": "never_valid_handoff"},
             checks=[_check_salvaged]),
    Scenario("no_change",
             "run-7f74e69c: a verified 'no changes needed' handoff was fatal",
             graphs=[_with_health], worker_policy={"F-HEALTH": "no_change"}),
    Scenario("companion_drop",
             "runs ff82d6e6/d918940d: a companion drop mid-build lost its leases",
             drop_before=("F-NOTES", 1), companion_restarts=2,
             checks=[_check_companion_starts(2), _check_claims_replayed, _check_under(25)]),
    Scenario("slow_planner",
             "run-a609a72c: a blocking task-graph call froze the loop and dropped the socket (1006)",
             planner_delay=6.0, keepalive=1.5, checks=[_check_companion_starts(1)]),
    Scenario("fix_loop", "the red-suite → blame → fixer → green path",
             worker_policy={"F-NOTES": "buggy"}, fixes=dict(FILES), checks=[_check_fixer_ran]),
]}


# ── the scripted model ───────────────────────────────────────────────────────

_ctx = threading.local()


def _text(content: str, chars: int) -> Completion:
    return Completion(content=content, model="sim", prompt_tokens=chars // 4,
                      completion_tokens=max(1, len(content) // 4))


def _calls(agent: str, step: int, chars: int, *calls: tuple[str, dict]) -> Completion:
    tool_calls = tuple(
        ModelToolCall(id=f"{agent}-{step}-{i}", name=name, arguments=json.dumps(args))
        for i, (name, args) in enumerate(calls)
    )
    return Completion(content="", model="sim", prompt_tokens=chars // 4,
                      completion_tokens=40 * len(calls), tool_calls=tool_calls)


def _task_of(messages: list[dict]) -> dict:
    for message in messages[1:3]:
        body = message.get("content") or ""
        start, end = body.find("<task>"), body.find("</task>")
        if start != -1 and end != -1:
            return json.loads(body[start + 6:end])
    return {}


class SimModel:
    """Answers each role the way a competent model would — except where told not to."""

    def __init__(self, scenario: Scenario, ctx: SimContext):
        self.s = scenario
        self.ctx = ctx
        self.lock = threading.Lock()
        self.calls: Counter[str] = Counter()
        self.prompt_tokens: Counter[str] = Counter()
        self.graph_calls = 0
        self.planner_retry_prompts: list[str] = []

    def respond(self, role: Role, agent: str, messages: list[dict], response_format, tools) -> Completion:
        chars = len(json.dumps(messages, default=str))
        with self.lock:
            self.calls[role.value] += 1
            self.prompt_tokens[role.value] += chars // 4
        schema = ((response_format or {}).get("json_schema") or {}).get("name", "")
        if agent.endswith("/compact"):
            return _text("## Objective\ncontinue the task", chars)
        if role is Role.REQUIREMENTS:
            return _text("ready", chars) if tools else _text(json.dumps(SPEC), chars)
        if role is Role.PLANNER:
            if tools or schema != "task_graph":
                return _text("ready to plan", chars)
            return self._graph(messages, chars)
        if role is Role.TEST_AUTHOR:
            return _text(json.dumps(SUITE), chars)
        step = sum(1 for m in messages if m.get("role") == "assistant")
        handoff = bool(tools) and len(tools) == 1
        if role is Role.REVIEWER:
            return _calls(agent, step, chars, ("submit_result", {"gaps": [], "summary": "clear (simulated)"}))
        if role is Role.REPORTER:
            return _calls(agent, step, chars, ("submit_result", {
                "readme": "# Sim Notes\n\nRun the API with uvicorn; tests with pytest.\n",
                "run_summary": "Simulated run.\n", "cost_report": "0 tokens (simulated).\n"}))
        if role is Role.FIXER:
            return self._fixer(agent, step, messages, chars, handoff)
        return self._worker(agent, step, messages, chars, handoff)

    def _graph(self, messages: list[dict], chars: int) -> Completion:
        with self.lock:
            index = self.graph_calls
            self.graph_calls += 1
        if index:
            self.planner_retry_prompts.append(messages[-1].get("content") or "")
        if self.s.planner_delay:
            time.sleep(self.s.planner_delay)  # a long blocking call, like a 19k-token graph
        graph = self.s.graphs[min(index, len(self.s.graphs) - 1)]
        return _text(json.dumps(graph), chars)

    def _worker(self, agent: str, step: int, messages: list[dict], chars: int, handoff: bool) -> Completion:
        task = _task_of(messages)
        tid, owned = task.get("id", ""), list(task.get("owned_paths", []))
        policy = self.s.worker_policy.get(tid, "default")
        if self.s.drop_before == (tid, step):
            self.ctx.drop_companion()
        submit = {"task_id": tid, "summary": f"{tid} done", "files_written": owned,
                  "what_works": "the suite's checks for this task", "known_gaps": []}
        if handoff:
            earlier = sum(1 for m in messages if m.get("role") == "assistant" and any(
                c["function"]["name"] == "submit_result" for c in m.get("tool_calls") or []))
            if policy == "never_valid_handoff" or (policy == "burn_then_empty_handoff" and not earlier):
                return _calls(agent, step, chars, ("submit_result", {}))  # the live failure
            return _calls(agent, step, chars, ("submit_result", submit))
        if step == 0:  # read before write (the ledger requires it for existing files)
            return _calls(agent, step, chars, *[("read_file", {"path": p}) for p in owned])
        if policy == "no_change":
            return _calls(agent, step, chars, ("submit_result", {**submit, "files_written": []}))
        if step == 1:
            source = BUGGY if policy == "buggy" else FILES
            return _calls(agent, step, chars, *[
                ("write_file", {"path": p, "content": source[p]}) for p in owned if p in source])
        if policy in ("burn_then_empty_handoff", "never_valid_handoff"):  # spend the whole budget
            return _calls(agent, step, chars, ("read_file", {"path": owned[0], "offset": step}))
        return _calls(agent, step, chars, ("submit_result", submit))

    def _fixer(self, agent: str, step: int, messages: list[dict], chars: int, handoff: bool) -> Completion:
        owned = list(_task_of(messages).get("owned_paths", []))
        targets = [p for p in owned if p in self.s.fixes]
        submit = {"diagnosis": "the notes route returned an envelope; the contract is a bare list",
                  "files_written": targets}
        if handoff or step >= 2:
            return _calls(agent, step, chars, ("submit_result", submit))
        if step == 0:
            return _calls(agent, step, chars, *[("read_file", {"path": p}) for p in owned])
        return _calls(agent, step, chars, *[
            ("write_file", {"path": p, "content": self.s.fixes[p]}) for p in targets])


# ── the simulated companion ──────────────────────────────────────────────────


class _UserPrompt(ApprovalPrompt):
    """The human at the companion: approves the plan when it is put to them.
    Shell/installs never get here (--auto-approve); counting asks proves the
    critical questions still reach the user."""

    def __init__(self, ctx: SimContext):
        super().__init__(interactive=False, output=lambda _: None)
        self.ctx = ctx

    def ask(self, request):
        self.ctx.user_asked.append(request.action)
        return Reply(True, "once")

    def ask_questions(self, agent_label, questions):
        self.ctx.user_asked.append("question")
        return [[] for _ in questions]


class SimExecutor(Executor):
    """The real Executor minus the host steps a simulation must not take."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._python_argv = [sys.executable]  # the host venv has pytest + fastapi

    def _probe_stack(self, tool_input, agent, call_id, notify):
        out = super()._probe_stack(tool_input, agent, call_id, notify)
        self._python_argv = [sys.executable]
        return out

    def _install_deps(self, tool_input, agent, call_id, notify):
        return "skipped (simulation: the host interpreter already has the stack)"

    def _runtime_smoke(self, tool_input, agent, call_id, notify):
        return json.dumps({"ok": True, "gaps": []})

    def _ensure_browser_runtime(self, tool_input, agent, call_id, notify):
        return json.dumps({"ready": True, "playwright_version": "sim", "chromium_version": "sim",
                           "installed": [], "note": "simulated"})

    def _browser_verify(self, tool_input, agent, call_id, notify):
        return json.dumps({"ok": True, "gaps": []})


@dataclass
class SimContext:
    workspace: Path
    settings: object = None
    registry: object = None
    loop: asyncio.AbstractEventLoop | None = None
    run_id: str = ""
    url: str = ""
    pair_code: str = ""
    restarts: int = 0
    companion_starts: int = 0
    user_asked: list[str] = field(default_factory=list)
    claims_replayed: list[int] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)

    def companion_supervisor(self) -> None:
        """Like the CLI watchdog: a FRESH Executor (empty memory) per connection."""
        from kiln.companion.client import serve

        while not self._stop.is_set() and self.companion_starts <= self.restarts:
            self.companion_starts += 1
            executor = SimExecutor(self.workspace, self.settings, prompt=_UserPrompt(self))
            asyncio.run(serve(self.url, self.pair_code, str(self.workspace), executor))
            time.sleep(0.2)

    def drop_companion(self) -> None:
        """Server-side close of the companion socket, then wait for the detach."""
        connection = self.registry.connections.get(self.run_id)
        if connection is None or self.loop is None:
            return
        asyncio.run_coroutine_threadsafe(connection.websocket.close(code=1011), self.loop).result(5)
        deadline = time.time() + 5
        while self.registry.connections.get(self.run_id) is connection and time.time() < deadline:
            time.sleep(0.05)


# ── running a scenario ───────────────────────────────────────────────────────


@dataclass
class SimResult:
    scenario: str
    status: str
    seconds: float
    model_calls: Counter
    est_prompt_tokens: Counter
    companion_starts: int
    user_asked: list[str]
    claims_replayed: list[int]
    top_level: list[str]
    transcript: list[dict]
    events: list[dict]
    planner_retry_prompts: list[str]
    loop_blocking_errors: list[str]
    failures: list[str] = field(default_factory=list)

    @property
    def green(self) -> bool:
        return self.status == "green"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records: list[str] = []

    def emit(self, record):
        if "event loop thread" in record.getMessage():
            self.records.append(record.getMessage())


@contextmanager
def _patched(model: SimModel, scenario: Scenario) -> Iterator[None]:
    import websockets

    from kiln.companion.registry import CompanionRegistry
    from kiln.gateway import client as gateway_module
    from kiln.stacks import scaffold_recipes

    real_replay = CompanionRegistry.replay_claims

    async def replay_claims(self, run_id):
        replayed = await real_replay(self, run_id)
        model.ctx.claims_replayed.append(replayed)
        return replayed

    CompanionRegistry.replay_claims = replay_claims
    real_complete = gateway_module.Gateway.complete
    real_transport = gateway_module.OpenAITransport.complete
    real_vite = scaffold_recipes.RECIPES.get("vite_react")
    real_connect = websockets.connect

    def gateway_complete(self, role, messages, **kwargs):
        _ctx.role, _ctx.agent = role, kwargs.get("agent_id") or role.value
        return real_complete(self, role, messages, **kwargs)

    def transport_complete(self, *, messages, response_format=None, tools=None, **_):
        return model.respond(_ctx.role, _ctx.agent, messages, response_format, tools)

    gateway_module.Gateway.complete = gateway_complete
    gateway_module.OpenAITransport.complete = transport_complete
    scaffold_recipes.RECIPES["vite_react"] = OFFLINE_VITE
    if scenario.keepalive:
        websockets.connect = functools.partial(
            real_connect, ping_interval=scenario.keepalive, ping_timeout=scenario.keepalive)
    try:
        yield
    finally:
        CompanionRegistry.replay_claims = real_replay
        gateway_module.Gateway.complete = real_complete
        gateway_module.OpenAITransport.complete = real_transport
        scaffold_recipes.RECIPES["vite_react"] = real_vite
        websockets.connect = real_connect


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_scenario(scenario: Scenario, workspace: Path, *, timeout: float = 240) -> SimResult:
    import urllib.request

    import uvicorn

    from kiln.config import Settings
    from kiln.main import create_app

    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    logs = workspace.parent / f"{workspace.name}-logs"
    ctx = SimContext(workspace=workspace, restarts=scenario.companion_restarts)
    model = SimModel(scenario, ctx)
    capture = _Capture()
    logging.getLogger("kiln.gateway").addHandler(capture)
    started = time.monotonic()
    status = "timeout"
    with _patched(model, scenario):
        settings = Settings(
            _env_file=None, model_api_key="sim", sandbox="host", agentic_devs=True,
            ask_requirements=False, enable_project_skills=False, auto_approve=True,
            debug=True, debug_log_dir=str(logs), max_parallel_devs=2,
        )
        ctx.settings = settings
        app = create_app(settings)
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))

        def serve_forever() -> None:
            ctx.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(ctx.loop)
            ctx.loop.run_until_complete(server.serve())

        server_thread = threading.Thread(target=serve_forever, daemon=True)
        server_thread.start()
        for _ in range(80):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        run = app.state.runs.create_run(IDEA)
        ctx.registry, ctx.run_id, ctx.pair_code = app.state.runs, run.id, run.pair_code
        ctx.url = f"ws://127.0.0.1:{port}/companion/ws"
        companion_thread = threading.Thread(target=ctx.companion_supervisor, daemon=True)
        companion_thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if run.status in ("green", "failed"):
                status = run.status
                break
            time.sleep(0.2)
        time.sleep(0.3)  # let the debug log flush its last lines
        ctx._stop.set()
        server.should_exit = True
        server_thread.join(10)
        companion_thread.join(10)
    logging.getLogger("kiln.gateway").removeHandler(capture)

    result = SimResult(
        scenario=scenario.name, status=status, seconds=round(time.monotonic() - started, 1),
        model_calls=model.calls, est_prompt_tokens=model.prompt_tokens,
        companion_starts=ctx.companion_starts, user_asked=list(ctx.user_asked),
        claims_replayed=list(ctx.claims_replayed),
        top_level=sorted(p.name for p in workspace.iterdir()),
        transcript=_jsonl(logs / run.id / "transcript.jsonl"),
        events=_jsonl(logs / run.id / "events.jsonl"),
        planner_retry_prompts=model.planner_retry_prompts,
        loop_blocking_errors=capture.records,
    )
    result.failures = _verify(scenario, result)
    return result


def _verify(scenario: Scenario, result: SimResult) -> list[str]:
    """The invariants every scenario must hold, plus its own checks."""
    failures = []
    if result.green != scenario.expect_green:
        stuck = [e.get("detail", "") for e in result.events if e.get("status") == "stuck"]
        failures.append(f"run {result.status} (expected {'green' if scenario.expect_green else 'not green'})"
                        + (f": {stuck[-1]}" if stuck else ""))
    stray = [name for name in result.top_level if name not in ALLOWED_TOP_LEVEL and not name.startswith(".")]
    if stray:
        failures.append(f"stray files at the workspace root: {stray}")
    if "review the plan and build" not in result.user_asked:
        failures.append("the plan beat never reached the user")
    if result.loop_blocking_errors:
        failures.append(f"model call on the event loop: {result.loop_blocking_errors[0][:120]}")
    for check in scenario.checks:
        problem = check(result)
        if problem:
            failures.append(problem)
    return failures
