"""Host-env product gaps must not force a fatal fixer round."""

from __future__ import annotations

import json
from pathlib import Path

from kiln.product.dev_server import _resolve_argv, _simple_node_script, resolve_start_command
from kiln.product.host_env import is_host_env_gap, partition_product_gaps


def test_playwright_missing_is_host_env():
    assert is_host_env_gap(
        "browser_verify: playwright is not installed (pip install playwright)"
    )
    assert is_host_env_gap("browser_verify: playwright browsers missing")


def test_spawn_failures_are_host_env():
    assert is_host_env_gap(
        "runtime_smoke: could not spawn start command (npm run start): "
        "executable not found on PATH: npm"
    )
    assert is_host_env_gap("FileNotFoundError: [WinError 2] The system cannot find the file specified")


def test_real_ui_gaps_stay_actionable():
    assert not is_host_env_gap("flow landing: timeout waiting for selector #signup")
    assert not is_host_env_gap("served_ui: no accent token in public/app.css")


def test_partition_drops_host_env_only():
    actionable, host, blocked = partition_product_gaps(
        [
            "playwright is not installed",
            "flow home: button missing",
        ]
    )
    assert actionable == ["flow home: button missing"]
    assert len(host) == 1
    assert blocked == []


def test_partition_separates_blocked_host_setup():
    from kiln.product.host_env import is_blocked_host_setup

    actionable, host, blocked = partition_product_gaps(
        [
            "browser_verify: blocked_host_setup — Playwright is not installed",
            "playwright is not installed",
            "flow home: button missing",
        ]
    )
    assert actionable == ["flow home: button missing"]
    assert len(host) == 1
    assert len(blocked) == 1
    assert is_blocked_host_setup(blocked[0])
    # a blocked gate is not an actionable code gap and not a mere host finding
    assert "blocked_host_setup" not in " ".join(actionable + host)


def test_simple_node_start_avoids_npm_wrapper(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"start": "node server.js"}, "main": "server.js"}),
        encoding="utf-8",
    )
    (tmp_path / "server.js").write_text("console.log('hi')\n", encoding="utf-8")
    cmd = resolve_start_command(tmp_path, 3456)
    assert cmd == ["node", "server.js"]


def test_simple_node_script_helper():
    assert _simple_node_script("node server.js") == ["node", "server.js"]
    assert _simple_node_script("vite --host") is None


def test_resolve_argv_finds_something_on_path():
    # python is always how we run tests; resolves to an absolute path
    resolved = _resolve_argv(["python", "-c", "pass"])
    assert Path(resolved[0]).is_file() or resolved[0].endswith(("python", "python.exe"))
