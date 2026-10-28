"""Live relay smoke: real server, real companion process, real tool calls.

Boots uvicorn on a scratch port, creates a run, points the real
`kiln-companion` CLI at a temp workspace, and drives file tools through the
relay. No model calls, no Docker needed. Run from the repo root:

    .venv\\Scripts\\python scripts/smoke_companion.py
"""

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PORT = 8031


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _wait_for_server(deadline: float = 15) -> None:
    start = time.time()
    while time.time() - start < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1)
            return
        except OSError:
            time.sleep(0.25)
    raise SystemExit("server did not come up")


def main() -> None:
    import uvicorn

    from kiln.config import Settings
    from kiln.main import create_app

    settings = Settings(_env_file=None, model_api_key="smoke", project_token_ceiling=5000)
    config = uvicorn.Config(
        create_app(settings), host="127.0.0.1", port=PORT, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_server()

    workspace = Path(tempfile.mkdtemp(prefix="kiln-smoke-"))
    try:
        created = _post("/runs", {"idea": "smoke test run"})
        print(f"run: {created['run_id']}  pair code: {created['pair_code']}")
        print(f"connect command shown to users: {created['connect']}")

        companion = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "kiln.companion.cli",
                "connect",
                created["pair_code"],
                "--dir",
                str(workspace),
                "--server",
                f"http://127.0.0.1:{PORT}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=ROOT,
        )
        time.sleep(2.5)  # let the handshake land

        hello = "print('hi from the team')\n"
        write = _post(
            f"/runs/{created['run_id']}/probe",
            {"tool": "write_file", "input": {"path": "app/hello.py", "content": hello}},
        )
        print(f"write: {write}")
        read = _post(
            f"/runs/{created['run_id']}/probe",
            {"tool": "read_file", "input": {"path": "app/hello.py"}},
        )
        print(f"read:  {read}")
        escape = _post(
            f"/runs/{created['run_id']}/probe",
            {"tool": "write_file", "input": {"path": "../escape.txt", "content": "nope"}},
        )
        print(f"escape attempt refused: {escape}")

        assert write.get("ok"), write
        assert read.get("output") == "print('hi from the team')\n", read
        assert escape.get("ok") is False, escape
        assert (workspace / "app" / "hello.py").is_file()
        assert not (workspace.parent / "escape.txt").exists()

        companion.terminate()
        print("\nRELAY SMOKE PASSED — the team can build on this machine")
    finally:
        server.should_exit = True
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    main()
