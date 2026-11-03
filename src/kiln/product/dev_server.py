"""Shared host-only app process for runtime_smoke and browser_verify."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            try:
                sock.connect(("127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.25)
    return False


def _resolve_argv(cmd: list[str]) -> list[str]:
    """Resolve argv[0] via PATH (npm.CMD / node.EXE on Windows)."""
    if not cmd:
        return cmd
    found = shutil.which(cmd[0])
    if found is None and os.name == "nt":
        found = shutil.which(f"{cmd[0]}.cmd") or shutil.which(f"{cmd[0]}.exe")
    if found is None:
        raise FileNotFoundError(f"executable not found on PATH: {cmd[0]}")
    return [found, *cmd[1:]]


def _simple_node_script(script: str) -> list[str] | None:
    """``node server.js`` (optional flags) → run node directly; PORT is in env."""
    parts = script.strip().split()
    if len(parts) >= 2 and parts[0] in {"node", "nodejs"} and parts[1].endswith(
        (".js", ".mjs", ".cjs")
    ):
        return ["node", *parts[1:]]
    return None


def _node_start(root: Path, port: int) -> list[str] | None:
    pkg = root / "package.json"
    if not pkg.is_file():
        return None
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    scripts = data.get("scripts") or {}
    if not isinstance(scripts, dict):
        return None
    env_port = str(port)
    for key in ("start", "dev", "serve", "preview"):
        script = scripts.get(key)
        if not script or not isinstance(script, str):
            continue
        direct = _simple_node_script(script)
        if direct:
            return direct
        return ["npm", "run", key, "--", "--host", "127.0.0.1", "--port", env_port]
    main = data.get("main")
    if isinstance(main, str) and main.endswith((".js", ".mjs", ".cjs")):
        return ["node", main]
    for candidate in ("server.js", "index.js", "app.js", "src/server.js"):
        if (root / candidate).is_file():
            return ["node", candidate]
    return None


def _python_start(root: Path, port: int) -> list[str] | None:
    for module_path, app_attr in (
        ("app.py", "app"),
        ("main.py", "app"),
        ("server.py", "app"),
        ("src/app.py", "app"),
    ):
        path = root / module_path
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        mod = module_path.replace("\\", "/").removesuffix(".py").replace("/", ".")
        if "fastapi" in text or "uvicorn" in text:
            return [
                "python",
                "-m",
                "uvicorn",
                f"{mod}:{app_attr}",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ]
        if "flask" in text:
            env_mod = mod
            return [
                "python",
                "-c",
                (
                    f"import os; os.environ['FLASK_APP']='{env_mod}:{app_attr}'; "
                    f"from flask.cli import main; "
                    f"import sys; sys.argv=['flask','run','--host','127.0.0.1',"
                    f"'--port','{port}']; main()"
                ),
            ]
    return None


def resolve_start_command(root: Path, port: int) -> list[str] | None:
    return _node_start(root, port) or _python_start(root, port)


@dataclass
class DevServer:
    root: Path
    port: int
    proc: subprocess.Popen[str] | None
    cmd: list[str] | None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def start_dev_server(root: Path, *, timeout: float = 20.0) -> tuple[DevServer | None, str | None]:
    """Start the app on a free port.

    Returns ``(server, None)`` on success, ``(None, None)`` when no start command
    is known, or ``(None, gap)`` when start was attempted but the port never opened.
    """
    root = Path(root)
    port = free_port()
    cmd = resolve_start_command(root, port)
    if not cmd:
        return None, None
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["HOST"] = "127.0.0.1"
    env.setdefault("BROWSER", "none")
    env.setdefault("CI", "1")
    try:
        argv = _resolve_argv(cmd)
        proc = subprocess.Popen(
            argv,
            cwd=root,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError as exc:
        return None, f"could not spawn start command ({' '.join(cmd)}): {exc}"
    except OSError as exc:
        return None, f"could not spawn start command ({' '.join(cmd)}): {exc}"
    server = DevServer(root=root, port=port, proc=proc, cmd=cmd)
    if not wait_port(port, timeout=timeout):
        gap = (
            f"server did not listen on 127.0.0.1:{port} "
            f"within {int(timeout)}s (cmd: {' '.join(cmd)})"
        )
        server.stop()
        return None, gap
    return server, None
