"""Eval-style evidence: a multi-root yes-app spec scaffolds and installs
deterministically through the companion — official fingerprints for argv
recipes, pinned trees for template recipes, per-root install routing.
"""

from pathlib import Path

from kiln.companion.executor import Executor
from kiln.companion.process import CommandResult
from kiln.companion.prompt import ApprovalPrompt, Reply
from kiln.companion.protocol import AgentScope
from kiln.config import Settings
from kiln.schemas.spec import Spec


class _Allow(ApprovalPrompt):
    def __init__(self):
        super().__init__(interactive=True, output=lambda _: None)

    def ask(self, request):
        return Reply(True, "once")


def _yes_app_spec() -> Spec:
    return Spec.model_validate(
        {
            "title": "Board",
            "summary": "A Kanban board with a python API and a React SPA.",
            "stack": "FastAPI + Vite/React",
            "stack_id": "python",
            "frameworks": ["fastapi", "vite_react"],
            "roots": [
                {"path": "backend", "role": "api", "framework": "fastapi"},
                {"path": "frontend", "role": "web", "framework": "vite_react"},
            ],
            "user_stories": [
                {"id": "s1", "as_a": "user", "i_want": "to move cards", "so_that": "I track work"}
            ],
            "acceptance_criteria": [
                {
                    "id": "c1",
                    "story_id": "s1",
                    "given": "the API is up",
                    "when": "GET /health",
                    "then": "200 ok",
                    "oracle": "tests/test_api.py — GET /health returns 200 {status:'ok'}",
                }
            ],
            "file_plan": [
                {"path": "backend/app/main.py", "purpose": "API entry"},
                {"path": "frontend/src/main.tsx", "purpose": "SPA mount"},
            ],
        }
    )


def test_multi_root_scaffold_and_install_flow(tmp_path, monkeypatch):
    executor = Executor(tmp_path, Settings(_env_file=None, sandbox="host"), prompt=_Allow())
    spec = _yes_app_spec()
    scope = AgentScope(id="orchestrator", label="Kiln", role="planner", owned_paths=[])

    ran: list[tuple[list[str], str]] = []

    def fake_run(argv, cwd, timeout=600, env=None):
        ran.append((list(argv), str(cwd)))
        if argv[:3] == ["npm", "create", "vite@6"]:
            # the create CLI builds its target folder inside its cwd (the staging dir)
            built = Path(cwd) / argv[3]
            built.mkdir(parents=True, exist_ok=True)
            (built / "vite.config.ts").write_text("export default {}\n")
            (built / "package.json").write_text('{"name":"frontend"}\n')
        return CommandResult(0, "ok")

    monkeypatch.setattr("kiln.companion.executor.run_argv", fake_run)

    # scaffold each root exactly as the orchestrator does
    for root in spec.roots:
        executor.execute(
            "scaffold_framework",
            {"framework": root.framework, "root": root.path, "options": {}},
            agent=scope,
        )

    # template recipe: real files on disk, official-shaped
    assert (tmp_path / "backend" / "app" / "main.py").is_file()
    assert "include_router" in (tmp_path / "backend" / "app" / "main.py").read_text()
    # argv recipe: the companion ran the pinned Vite command — no agent wrote configs
    assert any(a[:3] == ["npm", "create", "vite@6"] for a, _ in ran)
    assert (tmp_path / "frontend" / "vite.config.ts").is_file()

    # per-root install routes to the right toolchain at the right path
    for root in spec.roots:
        stack = "node" if root.framework == "vite_react" else "python"
        executor.execute("install_deps", {"stack_id": stack, "root": root.path}, agent=scope)
    assert any(a[:2] == ["npm", "install"] and cwd.endswith("frontend") for a, cwd in ran)
