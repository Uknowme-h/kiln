"""Browser runtime provisioning (PRP Phase 5.1) with injected probes."""

from __future__ import annotations

from kiln.product.browser_runtime import ensure_browser_runtime


def _fake_runner_ok():
    return lambda argv: (True, "ok")


def test_ready_when_playwright_and_chromium_already_launch():
    status = ensure_browser_runtime(
        _detect=lambda: (True, "1.49.0"),
        _launch=lambda: (True, "131.0.6778.33", False),
        _runner=_fake_runner_ok(),
    )
    assert status.ready is True
    assert status.playwright_version == "1.49.0"
    assert status.chromium_version == "131.0.6778.33"
    assert status.installed == []  # nothing needed installing


def test_installs_playwright_then_chromium_when_missing():
    calls = []

    def runner(argv):
        calls.append(" ".join(argv))
        return True, "ok"

    # 1st detect: missing; 2nd (after pip install): present
    detects = iter([(False, ""), (True, "1.49.0")])
    # 1st launch: missing browser; 2nd (after install): launches
    launches = iter([(False, "", True), (True, "131.0", False)])

    status = ensure_browser_runtime(
        _detect=lambda: next(detects),
        _launch=lambda: next(launches),
        _runner=runner,
    )
    assert status.ready is True
    assert status.installed == ["playwright", "chromium"]
    assert any("pip install playwright" in c for c in calls)
    assert any("playwright install chromium" in c for c in calls)


def test_only_chromium_is_installed_when_the_package_is_present():
    calls = []

    def runner(argv):
        calls.append(" ".join(argv))
        return True, "ok"

    launches = iter([(False, "", True), (True, "131.0", False)])
    status = ensure_browser_runtime(
        _detect=lambda: (True, "1.49.0"),
        _launch=lambda: next(launches),
        _runner=runner,
    )
    assert status.ready is True
    assert status.installed == ["chromium"]
    assert not any("pip install playwright" in c for c in calls)


def test_a_failed_pip_install_is_not_ready_and_says_why():
    status = ensure_browser_runtime(
        _detect=lambda: (False, ""),
        _launch=lambda: (False, "", True),
        _runner=lambda argv: (False, "permission denied"),
    )
    assert status.ready is False
    assert "pip install playwright failed" in status.note


def test_a_failed_chromium_install_is_not_ready_and_says_why():
    def runner(argv):
        if "playwright" in argv and "install" in argv and "chromium" in argv:
            return False, "network error"
        return True, "ok"

    status = ensure_browser_runtime(
        _detect=lambda: (True, "1.49.0"),
        _launch=lambda: (False, "", True),
        _runner=runner,
    )
    assert status.ready is False
    assert "chromium" in status.note


def test_install_false_never_runs_an_installer():
    calls = []

    def runner(argv):
        calls.append(argv)
        return True, "ok"

    status = ensure_browser_runtime(
        install=False,
        _detect=lambda: (False, ""),
        _launch=lambda: (False, "", True),
        _runner=runner,
    )
    assert status.ready is False
    assert status.note == "playwright package not installed"
    assert calls == []  # no installer ran without permission


def test_an_installed_but_unlaunchable_chromium_is_not_reinstalled():
    calls = []

    def runner(argv):
        calls.append(argv)
        return True, "ok"

    # launched=False but missing=False → crash on an installed binary: do not reinstall
    status = ensure_browser_runtime(
        _detect=lambda: (True, "1.49.0"),
        _launch=lambda: (False, "", False),
        _runner=runner,
    )
    assert status.ready is False
    assert "failed to launch" in status.note
    assert calls == []
