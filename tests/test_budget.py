import pytest

from kiln.gateway.budget import BudgetGuard
from kiln.gateway.errors import BudgetExceeded


def test_reserve_and_commit_releases_the_unused_portion():
    guard = BudgetGuard(100)
    guard.reserve(40)
    guard.commit(reserved=40, actual=15)
    assert guard.snapshot() == {"ceiling": 100, "used": 15, "reserved": 0, "remaining": 85}


def test_reserve_refuses_when_the_ceiling_would_be_passed():
    guard = BudgetGuard(100)
    guard.reserve(80)
    with pytest.raises(BudgetExceeded):
        guard.reserve(30)
    assert guard.snapshot()["reserved"] == 80


def test_release_returns_the_reservation():
    guard = BudgetGuard(50)
    guard.reserve(20)
    guard.release(20)
    assert guard.snapshot()["remaining"] == 50
