import json

import pytest

from kiln.companion.executor import ExecutionError, Executor, PermissionDenied
from kiln.companion.prompt import ApprovalPrompt, Reply
from kiln.companion.protocol import AgentScope
from kiln.config import Settings


class AlwaysAllow(ApprovalPrompt):
    def __init__(self):
        super().__init__(interactive=True, output=lambda _: None)

    def ask(self, request):
        return Reply(True, "once" if not request.allow_run_scope else "run")


@pytest.fixture()
def executor(tmp_path):
    return Executor(
        tmp_path,
        Settings(_env_file=None, sandbox="host"),
        prompt=AlwaysAllow(),
    )


def _worker(owned=("app/",)):
    return AgentScope(id="worker-1", label="Worker 1", role="worker", owned_paths=list(owned))


def test_scaffold_framework_template_writes_a_tree(executor):
    out = executor.execute(
        "scaffold_framework",
        {"framework": "fastapi", "root": "backend", "options": {}},
        agent=_worker(("backend/",)),
    )
    assert "scaffolded fastapi" in out
    main = executor.execute("read_file", {"path": "backend/app/main.py"})
    assert "include_router" in main
    assert (executor.root / "backend" / "requirements.txt").is_file()


def test_scaffold_framework_refuses_a_dirty_root(executor):
    executor.execute(
        "scaffold_framework",
        {"framework": "fastapi", "root": "backend", "options": {}},
        agent=_worker(("backend/",)),
    )
    # second run without overwrite must refuse: the manifest now exists
    with pytest.raises(ExecutionError, match="already scaffolded"):
        executor.execute(
            "scaffold_framework",
            {"framework": "fastapi", "root": "backend", "options": {}},
            agent=_worker(("backend/",)),
        )
    # explicit overwrite is allowed
    out = executor.execute(
        "scaffold_framework",
        {"framework": "fastapi", "root": "backend", "options": {"overwrite": True}},
        agent=_worker(("backend/",)),
    )
    assert "scaffolded fastapi" in out


def test_scaffold_framework_rejects_an_unknown_framework(executor):
    with pytest.raises((ExecutionError, ValueError), match="no scaffold recipe"):
        executor.execute(
            "scaffold_framework",
            {"framework": "rails", "root": "api", "options": {}},
            agent=_worker(("api/",)),
        )


def test_deferred_executor_browses_and_locks_a_workspace(tmp_path):
    nested = tmp_path / "apps" / "demo"
    nested.mkdir(parents=True)
    executor = Executor(
        None,
        Settings(_env_file=None, sandbox="host"),
        prompt=AlwaysAllow(),
    )
    with pytest.raises(ExecutionError, match="workspace not chosen"):
        executor.execute("read_file", {"path": "x.txt"})
    listing = json.loads(executor.execute("browse_host", {"path": str(tmp_path)}))
    assert listing["cwd"] == str(tmp_path.resolve())
    names = {entry["name"] for entry in listing["entries"]}
    assert "apps" in names
    locked = json.loads(
        executor.execute("set_workspace", {"path": str(nested), "create": False})
    )
    assert locked["workspace"] == str(nested.resolve())
    assert executor.root == nested.resolve()


def test_set_workspace_can_create_a_fresh_folder(tmp_path):
    executor = Executor(
        None,
        Settings(_env_file=None, sandbox="host"),
        prompt=AlwaysAllow(),
    )
    target = tmp_path / "brand-new"
    locked = json.loads(
        executor.execute("set_workspace", {"path": str(target), "create": True})
    )
    assert target.is_dir()
    assert locked["workspace"] == str(target.resolve())


def test_write_then_read_round_trips_inside_the_workspace(executor):
    agent = _worker()
    executor.execute("claim_scope", {"owned_paths": ["app/"]}, agent)
    result = executor.execute(
        "write_file", {"path": "app/main.py", "content": "print(1)\n"}, agent
    )
    assert "wrote app/main.py" in result
    # overwrite requires a prior read
    executor.execute("read_file", {"path": "app/main.py"}, agent)
    executor.execute(
        "write_file", {"path": "app/main.py", "content": "print(2)\n"}, agent
    )
    assert executor.execute("read_file", {"path": "app/main.py"}, agent) == "print(2)\n"


def test_paths_cannot_escape_the_workspace(executor):
    agent = _worker(["."])
    executor.execute("claim_scope", {"owned_paths": ["."]}, agent)
    with pytest.raises(Exception):
        executor.execute("write_file", {"path": "../outside.txt", "content": "x"}, agent)


def test_lease_blocks_a_peer_write(executor):
    w1 = _worker(["app/"])
    w2 = AgentScope(id="worker-2", label="Worker 2", role="worker", owned_paths=["api/"])
    executor.execute("claim_scope", {"owned_paths": ["app/"]}, w1)
    executor.execute("claim_scope", {"owned_paths": ["api/"]}, w2)
    with pytest.raises(Exception, match="Worker 1|owned"):
        executor.execute(
            "write_file", {"path": "app/main.py", "content": "x\n"}, w2
        )


def test_edit_file_needs_a_prior_read(executor):
    from kiln.companion.leases import ReadLedger

    agent = _worker()
    executor.execute("claim_scope", {"owned_paths": ["app/"]}, agent)
    executor.execute("write_file", {"path": "app/main.py", "content": "alpha\n"}, agent)
    # a write records what the agent put there; simulate an external change by
    # clearing the ledger so the next edit must re-read
    executor.reads = ReadLedger()
    with pytest.raises(ExecutionError, match="read"):
        executor.execute(
            "edit_file",
            {"path": "app/main.py", "old_string": "alpha", "new_string": "beta"},
            agent,
        )
    executor.execute("read_file", {"path": "app/main.py"}, agent)
    out = executor.execute(
        "edit_file",
        {"path": "app/main.py", "old_string": "alpha", "new_string": "beta"},
        agent,
    )
    assert "edited" in out
    assert "beta" in executor.execute("read_file", {"path": "app/main.py"}, agent)


def test_destructive_shell_is_refused(executor):
    agent = _worker(["."])
    executor.execute("claim_scope", {"owned_paths": ["."]}, agent)
    with pytest.raises(PermissionDenied, match="destructive"):
        executor.execute("shell", {"command": "rm -rf /"}, agent)


def test_a_failing_smoke_check_raises_instead_of_returning_advice(executor):
    # a broken file must fail loudly: the orchestrator treats the raise as a
    # broken layer, not as advisory output to discard
    agent = _worker(["."])
    executor.execute("claim_scope", {"owned_paths": ["."]}, agent)
    executor.execute(
        "write_file", {"path": "app/broken.py", "content": "def nope(:\n    pass\n"}, agent
    )
    with pytest.raises(ExecutionError, match="nope|SyntaxError|error"):
        executor.execute(
            "smoke_check", {"stack_id": "python", "paths": ["app/broken.py"]}, agent
        )


def test_write_file_appends_soft_diagnostics_for_syntax_errors(executor):
    agent = _worker(["."])
    executor.execute("claim_scope", {"owned_paths": ["."]}, agent)
    out = executor.execute(
        "write_file",
        {"path": "app/broken.py", "content": "def nope(:\n    pass\n"},
        agent,
    )
    assert "wrote app/broken.py" in out
    assert "Diagnostics after edit" in out or "SyntaxError" in out or "error" in out.lower()


def test_diagnostics_tool_returns_ok_when_healthy(executor):
    agent = _worker(["."])
    executor.execute("claim_scope", {"owned_paths": ["."]}, agent)
    executor.execute(
        "write_file", {"path": "app/main.py", "content": "print(1)\n"}, agent
    )
    out = executor.execute("diagnostics", {"paths": ["app/main.py"]}, agent)
    assert "ok" in out.lower() or out == ""


def test_missing_toolchain_does_not_crash_diagnostics(executor, tmp_path):
    agent = _worker(["."])
    executor.execute("claim_scope", {"owned_paths": ["."]}, agent)
    # markdown has no smoke commands — empty/ok, never raise
    executor.execute(
        "write_file", {"path": "notes.md", "content": "# hi\n"}, agent
    )
    out = executor.execute("diagnostics", {"paths": ["notes.md"]}, agent)
    assert isinstance(out, str)


def test_a_healthy_smoke_check_passes_through(executor):
    agent = _worker(["."])
    executor.execute("claim_scope", {"owned_paths": ["."]}, agent)
    executor.execute("write_file", {"path": "app/main.py", "content": "print(1)\n"}, agent)
    out = executor.execute(
        "smoke_check", {"stack_id": "python", "paths": ["app/main.py"]}, agent
    )
    assert isinstance(out, str) and out


def test_vendor_deps_only_for_docker_sandbox(executor):
    assert "only used" in executor.execute("vendor_deps", {})


def test_vendor_deps_downloads_when_docker_sandbox(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, sandbox="docker")
    executor = Executor(tmp_path, settings, prompt=AlwaysAllow())
    (tmp_path / "requirements.txt").write_text("PyJWT==2.10.1\n", encoding="utf-8")

    class FakePip:
        returncode = 0
        stderr = ""
        stdout = ""

        def __call__(self, args, **_kwargs):
            self.calls = args
            return self

    fake = FakePip()
    monkeypatch.setattr("subprocess.run", fake)
    result = executor.execute("vendor_deps", {})
    assert "vendored" in result
    assert "manylinux2014_x86_64" in fake.calls


def test_reading_a_missing_file_is_an_error(executor):
    with pytest.raises(ExecutionError):
        executor.execute("read_file", {"path": "nope.txt"})


def test_reading_a_missing_peer_owned_file_is_a_soft_miss_not_an_error(executor):
    """run-9f836de2: parallel workers crash-looped on reads of files a peer was
    still writing. A peer-owned path that does not exist yet must come back as
    guidance, not an ExecutionError."""
    w2 = AgentScope(id="worker-2", label="Worker 2", role="worker", owned_paths=["backend/auth.py"])
    executor.execute("claim_scope", {"owned_paths": ["backend/auth.py"], "task": "auth"}, w2)
    # worker-1 reads worker-2's not-yet-written file: soft miss, names the owner
    out = executor.execute("read_file", {"path": "backend/auth.py"}, _worker(["backend/"]))
    assert "does not exist yet" in out
    assert "Worker 2" in out
    assert "list_peers" in out or "known_gap" in out


def test_reading_a_missing_file_inside_your_own_lease_stays_an_error(executor):
    """The soft miss is only for *peer* files; a missing file you own yourself
    is still a plain error (you are the one who should have written it)."""
    agent = _worker(["app/"])
    executor.execute("claim_scope", {"owned_paths": ["app/"]}, agent)
    with pytest.raises(ExecutionError):
        executor.execute("read_file", {"path": "app/missing.py"}, agent)


def test_unknown_tool_is_an_error(executor):
    with pytest.raises(ExecutionError, match="unknown tool"):
        executor.execute("not_a_tool", {})


# ── the plan-approval beat ──


class ScriptedPrompt(ApprovalPrompt):
    """Answers from a script of (approved, feedback) so deny-with-reason is testable."""

    def __init__(self, replies):
        super().__init__(interactive=True, output=lambda _: None)
        self.replies = list(replies)
        self.seen = []

    def ask(self, request):
        self.seen.append(request)
        approved, feedback = self.replies.pop(0)
        return Reply(approved, "once", feedback)


def _planner():
    return AgentScope(
        id="planner", label="Pat Planner", role="planner", owned_paths=["docs/plan.md"]
    )


def _executor_with_prompt(tmp_path, prompt):
    return Executor(tmp_path, Settings(_env_file=None, sandbox="host"), prompt=prompt)


def test_confirm_plan_approved_returns_ok(tmp_path):
    prompt = ScriptedPrompt([(True, "")])
    executor = _executor_with_prompt(tmp_path, prompt)
    out = executor.execute("confirm_plan", {"lines": ["2 tasks:", "t1 owns app.py"]}, _planner())
    assert out == "plan approved"
    request = prompt.seen[0]
    assert request.action == "review the plan and build"
    assert not request.allow_run_scope  # a plan is never remembered


def test_confirm_plan_denied_carries_the_feedback(tmp_path):
    executor = _executor_with_prompt(tmp_path, ScriptedPrompt([(False, "add due dates")]))
    with pytest.raises(PermissionDenied, match="add due dates"):
        executor.execute("confirm_plan", {"lines": ["2 tasks"]}, _planner())


def test_confirm_plan_without_a_tty_is_denied(tmp_path):
    executor = _executor_with_prompt(
        tmp_path, ApprovalPrompt(interactive=False, output=lambda _: None)
    )
    with pytest.raises(PermissionDenied):
        executor.execute("confirm_plan", {"lines": ["2 tasks"]}, _planner())


def test_the_planner_scope_writes_both_plan_documents(tmp_path):
    """Regression: the orchestrator renders docs/contract.md under the planner's
    scope after the graph validates. The permission ruleset must allow it (not
    just the plan note), or the run dies right after planning."""
    executor = _executor_with_prompt(tmp_path, ScriptedPrompt([(True, "")]))
    agent = AgentScope(
        id="planner",
        label="Pat Planner",
        role="planner",
        owned_paths=["docs/plan.md", "docs/contract.md"],
    )
    executor.execute(
        "claim_scope", {"owned_paths": agent.owned_paths, "task": "plan"}, agent
    )
    # both plan documents are writable through the real lease + permission stack
    assert executor.execute(
        "write_file", {"path": "docs/contract.md", "content": "# contract\n"}, agent
    ).startswith("wrote docs/contract.md")
    assert executor.execute(
        "write_file", {"path": "docs/plan.md", "content": "# plan\n"}, agent
    ).startswith("wrote docs/plan.md")
    # ...and nothing else: still confined to the two plan documents
    # (permission denies it before the lease is even consulted)
    with pytest.raises(PermissionDenied, match="not allowed"):
        executor.execute("write_file", {"path": "docs/other.md", "content": "x"}, agent)
