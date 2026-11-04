"""Bug-2: a respawned companion must not refuse in-scope writes mid-build.

The run-d918940d failure: the planner claimed docs/, the companion died, the
watchdog respawned it (fresh Executor, empty LeaseTable/ReadLedger), and the
contract write failed with LeaseError. These tests pin the two halves of the
fix: the workspace-file cache (reload on bind) and the server claim replay.
"""

import pytest

from kiln.companion.executor import ExecutionError, Executor
from kiln.companion.prompt import ApprovalPrompt, Reply
from kiln.companion.protocol import AgentScope
from kiln.config import Settings


class _Allow(ApprovalPrompt):
    def __init__(self):
        super().__init__(interactive=True, output=lambda _: None)

    def ask(self, request):
        return Reply(True, "once")


def _planner():
    return AgentScope(
        id="planner", label="Pat Planner", role="planner",
        owned_paths=["docs/plan.md", "docs/contract.md"],
    )


def test_a_respawned_companion_restores_leases_and_reads(tmp_path):
    # 1) original companion: claim, read, write — then "dies"
    first = Executor(tmp_path, Settings(_env_file=None, sandbox="host"), prompt=_Allow())
    first.execute(
        "claim_scope",
        {"owned_paths": ["docs/"], "task": "plan", "status": "working"},
        agent=_planner(),
    )
    first.execute("write_file", {"path": "docs/plan.md", "content": "# plan v1\n"}, agent=_planner())

    # 2) respawn: a fresh Executor on the same workspace must reload the state
    second = Executor(tmp_path, Settings(_env_file=None, sandbox="host"), prompt=_Allow())
    out = second.execute(
        "write_file", {"path": "docs/contract.md", "content": "# contract\n"}, agent=_planner()
    )
    assert "wrote docs/contract.md" in out
    # and editing the earlier file (read-before-write) is not refused as never-read
    out = second.execute(
        "edit_file",
        {"path": "docs/plan.md", "old_string": "# plan v1", "new_string": "# plan v2"},
        agent=_planner(),
    )
    assert "edited docs/plan.md" in out


def test_a_respawned_companion_still_refuses_out_of_scope_writes(tmp_path):
    # a worker (edit-allowed by role) claims only backend/ — the restored lease
    # must still refuse a write to frontend/ (the lease boundary survives reload)
    def worker():
        return AgentScope(id="worker-1", label="Wren", role="worker", owned_paths=["backend/"])

    from kiln.companion.leases import LeaseError

    first = Executor(tmp_path, Settings(_env_file=None, sandbox="host"), prompt=_Allow())
    first.execute("claim_scope", {"owned_paths": ["backend/"], "task": "api"}, agent=worker())
    second = Executor(tmp_path, Settings(_env_file=None, sandbox="host"), prompt=_Allow())
    with pytest.raises(LeaseError, match="outside your owned paths"):
        second.execute("write_file", {"path": "frontend/index.ts", "content": "x"}, agent=worker())


def test_lease_snapshot_roundtrip():
    from kiln.companion.leases import LeaseTable, ReadLedger

    table = LeaseTable()
    table.claim("worker-1", "Worker 1", ["backend/"])
    snap = table.snapshot()
    restored = LeaseTable()
    restored.restore(snap)
    assert restored.prefixes_of("worker-1") == ("backend",)
    restored.check_write("worker-1", "backend/app/main.py")  # must not raise

    ledger = ReadLedger()
    ledger.record("worker-1", "backend/app/main.py", "print(1)\n")
    snap = ledger.snapshot()
    restored_ledger = ReadLedger()
    restored_ledger.restore(snap)
    assert restored_ledger.check("worker-1", "backend/app/main.py", "print(1)\n") is None
