"""Provision the host browser runtime for UI product gates (PRP Phase 5.1).

`browser_verify` can only exercise the served UI when the host has Playwright
(the pip package) and a Chromium build. When either is missing, the gate used
to silently pass and run-9f836de2 shipped an unmounted app behind it; now the
gate reports BLOCKED_HOST_SETUP (see `browser_verify`). This module is the
provisioning step that turns "blocked" into "ready" where the user allows it:

1. detect the Playwright package,
2. detect a launchable Chromium (and its version),
3. install what's missing after approval,
4. verify a headless launch,
5. report the installed versions (pinned by the Playwright package).

The subprocess and browser-launch calls are injectable so the detection logic
is testable without a real browser or network.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from dataclasses import dataclass, field

__all__ = ["BrowserStatus", "ensure_browser_runtime"]

# Install timeout: a Chromium download is ~150MB — generous but bounded.
_INSTALL_TIMEOUT_SECONDS = 600


@dataclass
class BrowserStatus:
    playwright_installed: bool = False
    playwright_version: str = ""
    chromium_launched: bool = False
    chromium_version: str = ""
    installed: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def ready(self) -> bool:
        """The gate can actually run: package present AND a headless launch works."""
        return self.playwright_installed and self.chromium_launched


# ── injectable probes (monkeypatched in tests) ──


def _detect_playwright() -> tuple[bool, str]:
    """(installed, version) of the Playwright pip package in this interpreter."""
    if importlib.util.find_spec("playwright") is None:
        return False, ""
    try:
        from importlib.metadata import version

        return True, version("playwright")
    except Exception:  # noqa: BLE001 — version metadata is best-effort
        return True, ""


def _launch_chromium() -> tuple[bool, str, bool]:
    """(launched, version, missing) for a headless Chromium.

    `missing` is True only when the failure is "the browser binary is not
    installed" — distinct from a launch crash on an installed binary.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "", True
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                return True, browser.version, False
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 — any launch failure is probed below
        message = str(exc).lower()
        missing = "executable doesn't exist" in message or "playwright install" in message
        return False, "", missing


def _run(argv: list[str]) -> tuple[bool, str]:
    """Run an install command; return (ok, combined_output)."""
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=_INSTALL_TIMEOUT_SECONDS,
        check=False,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode == 0, output.strip()


def ensure_browser_runtime(
    *,
    install: bool = True,
    _detect=_detect_playwright,
    _launch=_launch_chromium,
    _runner=_run,
) -> BrowserStatus:
    """Detect the browser runtime; install what's missing when `install`.

    Returns a BrowserStatus; `ready` is True only when a headless Chromium
    actually launched at the end. Install failures or a user-declined install
    leave `ready` False with the reason in `note`.
    """
    status = BrowserStatus()

    # 1. Playwright package.
    installed, pw_version = _detect()
    status.playwright_installed = installed
    status.playwright_version = pw_version
    if not installed:
        if not install:
            status.note = "playwright package not installed"
            return status
        ok, output = _runner([sys.executable, "-m", "pip", "install", "playwright"])
        if not ok:
            status.note = f"pip install playwright failed: {output[-300:]}"
            return status
        status.installed.append("playwright")
        installed, pw_version = _detect()
        status.playwright_installed = installed
        status.playwright_version = pw_version
        if not installed:
            status.note = "playwright still not importable after install"
            return status

    # 2. Launchable Chromium.
    launched, chromium_version, missing = _launch()
    status.chromium_launched = launched
    status.chromium_version = chromium_version
    if not launched:
        if not missing:
            status.note = "chromium is installed but failed to launch headless"
            return status
        if not install:
            status.note = "chromium build not installed"
            return status
        ok, output = _runner([sys.executable, "-m", "playwright", "install", "chromium"])
        if not ok:
            status.note = f"playwright install chromium failed: {output[-300:]}"
            return status
        status.installed.append("chromium")
        launched, chromium_version, _ = _launch()
        status.chromium_launched = launched
        status.chromium_version = chromium_version
        if not launched:
            status.note = "chromium still did not launch after install"
            return status

    if status.installed:
        status.note = "provisioned: " + ", ".join(status.installed)
    return status
