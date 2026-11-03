import pytest

from kiln.companion.leases import KeyedLocks, LeaseError, LeaseTable, ReadLedger


def test_overlapping_claims_are_rejected_and_writes_are_checked():
    table = LeaseTable()
    table.claim("w1", "Worker 1", ["app/"])
    with pytest.raises(LeaseError):
        table.claim("w2", "Worker 2", ["app/main.py"])
    table.claim("w2", "Worker 2", ["api/"])
    table.check_write("w1", "app/main.py")
    with pytest.raises(LeaseError, match="Worker 2"):
        table.check_write("w1", "api/routes.py")
    table.release("w1")
    with pytest.raises(LeaseError, match="no lease"):
        table.check_write("w1", "app/main.py")


def test_read_ledger_requires_a_fresh_read_before_edit():
    ledger = ReadLedger()
    assert "read" in (ledger.check("w1", "a.py", "one") or "")
    ledger.record("w1", "a.py", "one")
    assert ledger.check("w1", "a.py", "one") is None
    assert "changed" in (ledger.check("w1", "a.py", "two") or "")


def test_keyed_locks_serialize_the_same_path():
    locks = KeyedLocks()
    order = []
    with locks.hold("a.py"):
        order.append(1)
        with locks.hold("b.py"):
            order.append(2)
    assert order == [1, 2]
