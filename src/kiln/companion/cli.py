"""The companion command line.

``kiln-companion listen`` — generic listener: paste the UI pair code, then
pick the folder in the browser.

``kiln-companion connect CODE --dir .`` — power-user / smoke shortcut that
locks a workspace up front.
"""

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

from kiln.companion.client import serve
from kiln.companion.executor import Executor, looks_like_kiln_repo
from kiln.companion.shellenv import detect_environment
from kiln.config import load_settings

DEFAULT_SERVER = "ws://127.0.0.1:8000"


def _check_docker() -> str | None:
    if shutil.which("docker") is None:
        return (
            "docker is not on PATH. Install Docker Desktop, start it, "
            "or re-run without --sandbox docker to use the host instead."
        )
    return None


def _apply_sandbox(args) -> object:
    settings = load_settings()
    if args.sandbox is not None:
        settings = settings.model_copy(update={"sandbox": args.sandbox})
    if getattr(args, "auto_approve", False):
        settings = settings.model_copy(update={"auto_approve": True})
    if settings.sandbox == "docker":
        problem = _check_docker()
        if problem:
            raise SystemExit(problem)
    return settings


def _ws_url(server: str) -> str:
    server = server.rstrip("/")
    if server.startswith("http"):
        server = server.replace("http", "ws", 1)
    return f"{server}/companion/ws"


def _add_shared_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--server",
        default=DEFAULT_SERVER,
        help=f"Kiln server base URL (default: {DEFAULT_SERVER})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="build even inside the kiln repository itself",
    )
    parser.add_argument(
        "--sandbox",
        choices=("host", "docker"),
        default=None,
        help="where tests run: host (default) or docker (opt-in offline sandbox)",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help=(
            "auto-approve shell/install approval prompts so the run proceeds "
            "unattended. Only covers shell runs/installs — ask_user questions "
            "and the plan-approval beat still ask."
        ),
    )


def _cmd_listen(args) -> int:
    settings = _apply_sandbox(args)
    env = detect_environment()
    print("kiln-companion — listening")
    print(f"environment — {env.describe()}")
    print(f"sandbox — {settings.sandbox}")
    if settings.auto_approve:
        print("auto-approve ON — shell/install approvals auto-run; questions and plan still ask")
    print("Paste the pair code from the Kiln UI, then choose a folder in the browser.")
    try:
        raw = input("Pair code: ").strip()
    except EOFError:
        raise SystemExit("no pair code provided") from None
    code = raw.upper().replace(" ", "")
    if not code:
        raise SystemExit("no pair code provided")

    try:
        executor = Executor(None, settings, environment=env, force=args.force)
    except Exception as exc:
        raise SystemExit(str(exc)) from exc

    print("commands that need approval will appear in this terminal")
    return asyncio.run(serve(_ws_url(args.server), code, "", executor))


def _cmd_connect(args) -> int:
    if args.new:
        workspace = Path(args.new)
        if workspace.exists() and any(workspace.iterdir()):
            raise SystemExit(f"--new folder is not empty: {workspace}")
        workspace.mkdir(parents=True, exist_ok=True)
    else:
        workspace = Path(args.dir)
        if not workspace.is_dir():
            raise SystemExit(f"no such folder: {workspace}")

    if looks_like_kiln_repo(workspace) and not args.force:
        raise SystemExit(
            "this looks like the kiln repository itself — generated files would "
            "land inside it (overwriting README.md and polluting tests/). "
            "Pick an empty folder or use --new my-app. Pass --force to proceed anyway."
        )

    settings = _apply_sandbox(args)
    env = detect_environment()
    try:
        executor = Executor(workspace, settings, environment=env, force=args.force)
    except Exception as exc:
        raise SystemExit(str(exc)) from exc

    print(f"kiln-companion — workspace {executor.root}")
    print(f"environment — {env.describe()}")
    print(f"sandbox — {settings.sandbox}")
    if settings.auto_approve:
        print("auto-approve ON — shell/install approvals auto-run; questions and plan still ask")
    else:
        print("commands that need approval will appear in this terminal")
    return asyncio.run(
        serve(
            _ws_url(args.server),
            args.code.strip().upper(),
            str(executor.root),
            executor,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="kiln-companion",
        description="Connect this machine to a Kiln run so the team can build here.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    listen = sub.add_parser(
        "listen",
        help="wait for a pair code, then let the UI pick the workspace folder",
    )
    _add_shared_flags(listen)
    listen.set_defaults(func=_cmd_listen)

    connect = sub.add_parser(
        "connect",
        help="claim a pair code with a folder already chosen (smoke / power users)",
    )
    connect.add_argument("code", help="the pair code shown by the web UI")
    target = connect.add_mutually_exclusive_group()
    target.add_argument("--dir", default=".", help="existing folder to build in (default: .)")
    target.add_argument("--new", help="create a fresh folder and build in it")
    _add_shared_flags(connect)
    connect.set_defaults(func=_cmd_connect)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
