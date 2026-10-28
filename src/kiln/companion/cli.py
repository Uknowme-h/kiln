"""The companion command line: `kiln-companion connect CODE --dir .`

Checks the local prerequisites (workspace, Docker) before connecting, so the
user gets an exact fix instruction instead of a mid-run failure.
"""

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

from kiln.companion.client import serve
from kiln.companion.executor import Executor
from kiln.config import load_settings

DEFAULT_SERVER = "ws://127.0.0.1:8000"


def _check_docker() -> str | None:
    if shutil.which("docker") is None:
        return (
            "docker is not on PATH. Install Docker Desktop, start it, "
            "and re-run this command."
        )
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="kiln-companion",
        description="Connect this machine to a Kiln run so the team can build here.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    connect = sub.add_parser("connect", help="claim a pair code and serve tool calls")
    connect.add_argument("code", help="the pair code shown by the web UI")
    target = connect.add_mutually_exclusive_group()
    target.add_argument("--dir", default=".", help="existing folder to build in (default: .)")
    target.add_argument("--new", help="create a fresh folder and build in it")
    connect.add_argument(
        "--server",
        default=DEFAULT_SERVER,
        help=f"Kiln server base URL (default: {DEFAULT_SERVER})",
    )
    args = parser.parse_args()

    if args.new:
        workspace = Path(args.new)
        if workspace.exists() and any(workspace.iterdir()):
            raise SystemExit(f"--new folder is not empty: {workspace}")
        workspace.mkdir(parents=True, exist_ok=True)
    else:
        workspace = Path(args.dir)
        if not workspace.is_dir():
            raise SystemExit(f"no such folder: {workspace}")

    problem = _check_docker()
    if problem:
        raise SystemExit(problem)

    settings = load_settings()
    try:
        executor = Executor(workspace, settings)
    except Exception as exc:
        raise SystemExit(str(exc)) from exc

    server = args.server.rstrip("/")
    url = f"{server.replace('http', 'ws', 1) if server.startswith('http') else server}/companion/ws"
    print(f"kiln-companion — workspace {executor.root}")
    exit_code = asyncio.run(serve(url, args.code.strip().upper(), str(executor.root), executor))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
