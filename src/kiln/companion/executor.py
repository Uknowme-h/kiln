"""Tool execution inside one user-chosen directory.

This is the only code that touches the user's disk, runs shell commands, or
reaches the network on their behalf. Every path is re-resolved under the
workspace root. Writes are checked against path leases. Shell, install, and
web tools ask the user in this terminal before they run.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import threading
from collections.abc import Callable
from pathlib import Path

from kiln.agents.definitions import DEFINITIONS
from kiln.agents.roles import Role
from kiln.companion.commandscan import scan_command
from kiln.companion.leases import KeyedLocks, LeaseError, LeaseTable, ReadLedger
from kiln.companion.permissions import PermissionDenied, PermissionEngine
from kiln.companion.process import (
    DEFAULT_TIMEOUT_SECONDS,
    BackgroundJobs,
    run_argv,
    run_shell,
    scrubbed_env,
    shell_argv,
)
from kiln.companion.prompt import ApprovalPrompt, ApprovalRequest, Question
from kiln.companion.protocol import AgentScope, ToolProgress
from kiln.companion.shellenv import EnvironmentInfo, detect_environment, refresh_windows_path
from kiln.companion.web import fetch_url, search_web
from kiln.config import Settings
from kiln.paths import safe_relative_path
from kiln.permissions import Ruleset
from kiln.sandbox.parser import parse_junit
from kiln.sandbox.runner import run_tests as run_tests_docker
from kiln.stacks.profiles import (
    RESULTS_FILE,
    install_command,
    parse_version,
    profile_for,
)
from kiln.tools import schemas as tool_schemas

Notify = Callable[[ToolProgress], None]

# Re-exported so the client can catch refusals without importing permissions.
__all__ = ["ExecutionError", "Executor", "PermissionDenied", "looks_like_kiln_repo"]


class ExecutionError(RuntimeError):
    """A tool input was invalid or the execution failed."""


def _role_rules(role: str) -> Ruleset:
    try:
        return DEFINITIONS[Role(role)].permissions
    except (KeyError, ValueError):
        return ()


def _fingerprint(root: Path) -> dict[str, str]:
    """sha256 of every regular file under root, workspace-relative paths."""
    out: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith((".kiln/", ".venv/", "node_modules/", "vendor/", ".deps/")):
            continue
        try:
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return out


_BLOCKED_BROWSE_MARKERS = (
    "windows/system32",
    "windows\\system32",
    "/etc/",
    "/sys/",
    "/proc/",
)


def looks_like_kiln_repo(workspace: Path) -> bool:
    """Building inside the kiln checkout would overwrite its README and pollute
    its tests/ with generated files."""
    pyproject = workspace / "pyproject.toml"
    if not (workspace / "src" / "kiln").is_dir() or not pyproject.is_file():
        return False
    return "kiln" in pyproject.read_text(encoding="utf-8", errors="ignore")[:2000].lower()


class Executor:
    def __init__(
        self,
        workspace: Path | None = None,
        settings: Settings | None = None,
        prompt: ApprovalPrompt | None = None,
        environment: EnvironmentInfo | None = None,
        *,
        force: bool = False,
    ):
        self.settings = settings or Settings(_env_file=None)
        self.env = environment or detect_environment()
        self.force = force
        # remote=True: approvals can be answered from the web floor as well as
        # this terminal; whichever the user answers first wins
        self.prompt = prompt or ApprovalPrompt(remote=True)
        self.permissions = PermissionEngine(self.prompt)
        self.leases = LeaseTable()
        self.locks = KeyedLocks()
        self.reads = ReadLedger()
        self.install_lock = threading.Lock()
        self.stack_id = "python"
        self._python_argv: list[str] | None = None
        self._peers: dict[str, dict] = {}
        self._peers_lock = threading.Lock()
        self.root: Path | None = None
        self.background: BackgroundJobs | None = None
        if workspace is not None:
            self.bind_workspace(workspace, force=force)

    def bind_workspace(self, workspace: Path, *, force: bool | None = None) -> Path:
        """Lock the build folder. Used by connect (up front) and listen (after UI pick)."""
        root = workspace.expanduser().resolve()
        if not root.is_dir():
            raise ExecutionError(f"workspace does not exist: {workspace}")
        allow = self.force if force is None else force
        if looks_like_kiln_repo(root) and not allow:
            raise ExecutionError(
                "this looks like the kiln repository itself — generated files would "
                "land inside it (overwriting README.md and polluting tests/). "
                "Pick another folder, or pass --force to proceed anyway."
            )
        if self.background is not None:
            self.background.stop_all()
        self.root = root
        self.background = BackgroundJobs(self.root / ".kiln" / "jobs")
        return self.root

    def close(self) -> None:
        if self.background is not None:
            self.background.stop_all()

    def _require_root(self) -> Path:
        if self.root is None:
            raise ExecutionError("workspace not chosen yet — pick a folder in the Kiln UI")
        return self.root

    def answer_approval(self, answer) -> bool:
        """The web floor's reply to the pending approval or ask_user questionnaire.
        False when nothing is waiting (the terminal answered first, or too late)."""
        from kiln.companion.prompt import Reply

        if getattr(answer, "answers", None) is not None:
            return self.prompt.answer_questions_remotely(list(answer.answers))
        scope = answer.scope if answer.scope in ("once", "run") else "once"
        return self.prompt.answer_remotely(
            Reply(bool(answer.approved), scope, str(answer.feedback or ""))
        )

    def environment_report(self) -> dict:
        return self.env.to_dict()

    def set_peers(self, peers: dict[str, dict]) -> None:
        with self._peers_lock:
            self._peers = dict(peers)

    def _resolve(self, raw_path: str) -> tuple[str, Path]:
        root = self._require_root()
        relative = "." if raw_path in ("", ".") else safe_relative_path(raw_path)
        candidate = (root / ("" if relative == "." else relative)).resolve()
        if candidate != root and root not in candidate.parents:
            raise ExecutionError(f"path escapes the workspace: {raw_path}")
        return relative, candidate

    def _browse_target(self, raw_path: str) -> Path:
        """Resolve a host path for the UI folder picker. Empty → user home."""
        text = (raw_path or "").strip()
        if not text:
            return Path(self.env.home).expanduser().resolve()
        path = Path(text).expanduser()
        try:
            path = path.resolve()
        except OSError as exc:
            raise ExecutionError(f"cannot resolve path: {raw_path}") from exc
        marker = path.as_posix().lower()
        for blocked in _BLOCKED_BROWSE_MARKERS:
            if blocked in marker or marker.endswith(blocked.rstrip("/\\")):
                raise ExecutionError(f"browse refused for system path: {path}")
        if not path.exists():
            raise ExecutionError(f"no such path: {path}")
        if not path.is_dir():
            raise ExecutionError(f"not a directory: {path}")
        return path

    def _agent_bits(self, agent: AgentScope | None) -> tuple[str, str, str, list[str], Ruleset]:
        if agent is None:
            return "orchestrator", "orchestrator", "", [], ()
        return (
            agent.id,
            agent.label or agent.id,
            agent.role,
            list(agent.owned_paths),
            _role_rules(agent.role),
        )

    def execute(
        self,
        tool: str,
        tool_input: dict,
        agent: AgentScope | None = None,
        call_id: str = "",
        notify: Notify | None = None,
    ) -> str:
        handlers = {
            "browse_host": self._browse_host,
            "set_workspace": self._set_workspace,
            "read_file": self._read_file,
            "list_dir": self._list_dir,
            "glob": self._glob,
            "grep": self._grep,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
            "shell": self._shell,
            "ask_user": self._ask_user,
            "web_fetch": self._web_fetch,
            "web_search": self._web_search,
            "probe_stack": self._probe_stack,
            "install_tool": self._install_tool,
            "install_deps": self._install_deps,
            "run_tests": self._run_tests,
            "smoke_check": self._smoke_check,
            "served_ui_audit": self._served_ui_audit,
            "runtime_smoke": self._runtime_smoke,
            "browser_verify": self._browser_verify,
            "diagnostics": self._diagnostics,
            "claim_scope": self._claim_scope,
            "release_scope": self._release_scope,
            "list_peers": self._list_peers,
            "confirm_plan": self._confirm_plan,
            "vendor_deps": self._vendor_deps_legacy,
        }
        handler = handlers.get(tool)
        if handler is None:
            raise ExecutionError(f"unknown tool: {tool}")
        if tool not in ("browse_host", "set_workspace"):
            self._require_root()
        return handler(tool_input, agent, call_id, notify)

    # ── listen-mode host browse / workspace lock ─────────────────────────────

    def _browse_host(self, tool_input, agent, call_id, notify) -> str:
        raw = tool_input.get("path", "") if isinstance(tool_input, dict) else ""
        if not isinstance(raw, str):
            raw = ""
        cwd = self._browse_target(raw)
        parent = str(cwd.parent) if cwd.parent != cwd else ""
        entries: list[dict] = []
        try:
            children = sorted(cwd.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            raise ExecutionError(f"cannot list {cwd}: {exc}") from exc
        for child in children:
            try:
                is_dir = child.is_dir()
            except OSError:
                continue
            if not is_dir:
                continue  # folder picker only needs directories
            # skip hidden / system-ish names in the picker
            if child.name.startswith(".") and child.name not in (".", ".."):
                continue
            entries.append({"name": child.name, "path": str(child), "is_dir": True})
        roots: list[dict] = [{"name": "Home", "path": str(Path(self.env.home).expanduser())}]
        if self.env.os == "Windows":
            import string

            for letter in string.ascii_uppercase:
                drive = Path(f"{letter}:\\")
                if drive.exists():
                    roots.append({"name": f"{letter}:", "path": str(drive)})
        return json.dumps(
            {
                "cwd": str(cwd),
                "parent": parent,
                "entries": entries,
                "roots": roots,
            }
        )

    def _set_workspace(self, tool_input, agent, call_id, notify) -> str:
        if not isinstance(tool_input, dict):
            raise ExecutionError("set_workspace needs {path}")
        raw = tool_input.get("path", "")
        create = bool(tool_input.get("create", False))
        if not isinstance(raw, str) or not raw.strip():
            raise ExecutionError("set_workspace needs a path")
        path = Path(raw.strip()).expanduser()
        if create:
            path.mkdir(parents=True, exist_ok=True)
        elif not path.is_dir():
            raise ExecutionError(f"no such folder: {path}")
        root = self.bind_workspace(path, force=self.force)
        return json.dumps({"workspace": str(root)})

    # ── filesystem ──────────────────────────────────────────────────────────

    def _read_file(self, tool_input, agent, call_id, notify) -> str:
        validated = tool_schemas.ReadFile.model_validate(tool_input)
        agent_id, label, _, _, role_rules = self._agent_bits(agent)
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action="read",
            resource=validated.path,
            role_rules=role_rules,
            call_id=call_id,
            notify=notify,
        )
        _, path = self._resolve(validated.path)
        if not path.is_file():
            raise ExecutionError(f"no such file in the workspace: {validated.path}")
        text = path.read_text(encoding="utf-8")
        self.reads.record(agent_id, validated.path, text)
        lines = text.splitlines(keepends=True)
        if validated.offset is not None or validated.limit is not None:
            start = (validated.offset or 1) - 1
            end = start + validated.limit if validated.limit else len(lines)
            chunk = "".join(lines[start:end])
            return f"# lines {start + 1}-{min(end, len(lines))} of {len(lines)}\n{chunk}"
        return text

    def _list_dir(self, tool_input, agent, call_id, notify) -> str:
        validated = tool_schemas.ListDir.model_validate(tool_input)
        agent_id, label, _, _, role_rules = self._agent_bits(agent)
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action="list",
            resource=validated.path,
            role_rules=role_rules,
            call_id=call_id,
            notify=notify,
        )
        _, path = self._resolve(validated.path)
        if not path.is_dir():
            raise ExecutionError(f"no such directory: {validated.path}")
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        lines = []
        for entry in entries[:200]:
            suffix = "/" if entry.is_dir() else ""
            lines.append(f"{entry.name}{suffix}")
        return "\n".join(lines) or "(empty)"

    def _glob(self, tool_input, agent, call_id, notify) -> str:
        validated = tool_schemas.Glob.model_validate(tool_input)
        agent_id, label, _, _, role_rules = self._agent_bits(agent)
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action="glob",
            resource=validated.pattern,
            role_rules=role_rules,
            call_id=call_id,
            notify=notify,
        )
        _, base = self._resolve(validated.path)
        matches = []
        for path in sorted(base.glob(validated.pattern)):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved != self.root and self.root not in resolved.parents:
                continue
            matches.append(path.relative_to(self.root).as_posix())
            if len(matches) >= 200:
                break
        return "\n".join(matches) or "(no matches)"

    def _grep(self, tool_input, agent, call_id, notify) -> str:
        validated = tool_schemas.Grep.model_validate(tool_input)
        agent_id, label, _, _, role_rules = self._agent_bits(agent)
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action="grep",
            resource=validated.pattern,
            role_rules=role_rules,
            call_id=call_id,
            notify=notify,
        )
        try:
            regex = re.compile(validated.pattern)
        except re.error as exc:
            raise ExecutionError(f"invalid regex: {exc}") from exc
        _, base = self._resolve(validated.path)
        hits: list[str] = []
        files = base.rglob("*") if base.is_dir() else [base]
        for path in files:
            if not path.is_file():
                continue
            rel = path.relative_to(self.root).as_posix()
            if validated.include and not fnmatch.fnmatch(rel, validated.include):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"{rel}:{number}:{line[:200]}")
                    if len(hits) >= 80:
                        return "\n".join(hits) + "\n…[more matches truncated]"
        return "\n".join(hits) or "(no matches)"

    def _write_file(self, tool_input, agent, call_id, notify) -> str:
        validated = tool_schemas.WriteFile.model_validate(tool_input)
        agent_id, label, _, owned, role_rules = self._agent_bits(agent)
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action="edit",
            resource=validated.path,
            role_rules=role_rules,
            call_id=call_id,
            notify=notify,
        )
        if owned or agent_id in self.leases.active_agents():
            self.leases.check_write(agent_id, validated.path)
        relative, path = self._resolve(validated.path)
        with self.locks.hold(relative):
            if path.exists():
                current = path.read_text(encoding="utf-8")
                problem = self.reads.check(agent_id, relative, current)
                if problem:
                    raise ExecutionError(problem)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(validated.content, encoding="utf-8")
            self.reads.record(agent_id, relative, validated.content)
        msg = f"wrote {relative} ({len(validated.content)} bytes)"
        diag = self._post_edit_diagnostics(relative)
        return f"{msg}\n{diag}" if diag else msg

    def _edit_file(self, tool_input, agent, call_id, notify) -> str:
        validated = tool_schemas.EditFile.model_validate(tool_input)
        if validated.old_string == validated.new_string:
            raise ExecutionError("old_string and new_string must differ")
        agent_id, label, _, owned, role_rules = self._agent_bits(agent)
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action="edit",
            resource=validated.path,
            role_rules=role_rules,
            call_id=call_id,
            notify=notify,
        )
        if owned or agent_id in self.leases.active_agents():
            self.leases.check_write(agent_id, validated.path)
        relative, path = self._resolve(validated.path)
        with self.locks.hold(relative):
            if not path.is_file():
                raise ExecutionError(f"no such file: {relative}")
            current = path.read_text(encoding="utf-8")
            problem = self.reads.check(agent_id, relative, current)
            if problem:
                raise ExecutionError(problem)
            count = current.count(validated.old_string)
            if count == 0:
                raise ExecutionError("old_string was not found in the file")
            if count > 1 and not validated.replace_all:
                raise ExecutionError(
                    f"old_string appears {count} times; pass replace_all=true or make it unique"
                )
            updated = (
                current.replace(validated.old_string, validated.new_string)
                if validated.replace_all
                else current.replace(validated.old_string, validated.new_string, 1)
            )
            path.write_text(updated, encoding="utf-8")
            self.reads.record(agent_id, relative, updated)
        msg = f"edited {relative} ({count} replacement{'s' if count != 1 else ''})"
        diag = self._post_edit_diagnostics(relative)
        return f"{msg}\n{diag}" if diag else msg

    def _diagnostic_commands(self, paths: list[str]) -> list[list[str]]:
        profile = profile_for(self.stack_id)
        commands = list(
            profile.smoke_commands(self.root, paths, self.env.os, self._python_argv)
            or []
        )
        # Optional richer checkers when present on PATH / in the project.
        bin_dir = self.root / ".venv" / ("Scripts" if self.env.os == "Windows" else "bin")
        for path in paths:
            if path.endswith(".py"):
                ruff = bin_dir / ("ruff.exe" if self.env.os == "Windows" else "ruff")
                if ruff.is_file():
                    commands.append([str(ruff), "check", path])
                pyright = bin_dir / ("pyright.exe" if self.env.os == "Windows" else "pyright")
                if pyright.is_file():
                    commands.append([str(pyright), path])
                elif (self.root / "node_modules" / ".bin" / (
                    "pyright.cmd" if self.env.os == "Windows" else "pyright"
                )).is_file():
                    commands.append(
                        [
                            str(
                                self.root
                                / "node_modules"
                                / ".bin"
                                / ("pyright.cmd" if self.env.os == "Windows" else "pyright")
                            ),
                            path,
                        ]
                    )
            if path.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")):
                eslint = self.root / "node_modules" / ".bin" / (
                    "eslint.cmd" if self.env.os == "Windows" else "eslint"
                )
                if eslint.is_file():
                    commands.append([str(eslint), path])
        return commands

    def _run_diagnostics(self, paths: list[str], *, label: str = "diagnostics") -> str:
        try:
            commands = self._diagnostic_commands(paths)
        except Exception:
            return ""
        if not commands:
            return ""
        parts: list[str] = []
        for cmd in commands:
            try:
                result = run_argv(cmd, self.root, timeout=60)
            except Exception as exc:  # noqa: BLE001
                parts.append(f"{label} error: {exc}")
                continue
            rendered = result.render()
            if len(rendered) > 4000:
                rendered = rendered[:3800] + "\n…[diagnostics clipped]"
            if not result.ok:
                parts.append(f"{label} ({' '.join(cmd)}):\n{rendered}")
            elif rendered.strip():
                # Quiet success — only surface noise if the tool printed something useful.
                pass
        return "\n".join(parts)

    def _post_edit_diagnostics(self, relative: str) -> str:
        """Best-effort syntax/type check after a write (OpenCode LSP seam).

        Failures are appended to the tool result so the model can self-correct;
        they do not raise — the suite remains the hard gate.
        """
        # Honored when Settings.post_edit_diagnostics is true on the server;
        # companion always runs cheap checks — empty when no commands apply.
        return self._run_diagnostics([relative], label="Diagnostics after edit")

    def _diagnostics(self, tool_input, agent, call_id, notify) -> str:
        validated = tool_schemas.Diagnostics.model_validate(tool_input or {})
        paths = list(validated.paths or [])
        if not paths:
            return "diagnostics: no paths provided"
        agent_id, label, _, _, role_rules = self._agent_bits(agent)
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action="shell",
            resource="diagnostics",
            role_rules=role_rules,
            call_id=call_id,
            notify=notify,
        )
        text = self._run_diagnostics(paths, label="diagnostics")
        return text or "diagnostics: ok (no findings)"

    # ── shell ───────────────────────────────────────────────────────────────

    def _shell(self, tool_input, agent, call_id, notify) -> str:
        validated = tool_schemas.Shell.model_validate(tool_input)
        agent_id, label, _, owned, role_rules = self._agent_bits(agent)
        scan = scan_command(validated.command, self.env.shell.kind)
        if scan.destructive:
            raise self.permissions.deny(
                agent_id, "shell", validated.command, "refused: destructive command"
            )
        action = "install" if scan.installs else "shell"
        note = ""
        if scan.opaque:
            note = f"could not fully parse ({scan.reason}); shown raw, cannot be remembered"
        elif scan.installs:
            note = "installs or upgrades software"
        elif scan.network:
            note = "talks to the network"
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action=action,
            resource=validated.command,
            role_rules=role_rules,
            request=ApprovalRequest(
                agent_label=label,
                action=action,
                lines=scan.lines,
                shell=self.env.shell.label,
                workdir=validated.workdir or ".",
                note=note,
                allow_run_scope=scan.rememberable,
            ),
            remember_key=validated.command if scan.rememberable else None,
            call_id=call_id,
            notify=notify,
        )
        workdir_rel = validated.workdir or "."
        _, cwd = self._resolve(workdir_rel)
        if not cwd.is_dir():
            raise ExecutionError(f"workdir does not exist: {workdir_rel}")
        timeout = (validated.timeout_ms or DEFAULT_TIMEOUT_SECONDS * 1000) / 1000
        before = _fingerprint(self.root) if owned or agent_id in self.leases.active_agents() else None
        if validated.background:
            job_id, log_path = self.background.start(
                shell_argv(self.env.shell, validated.command),
                cwd,
                scrubbed_env(),
            )
            return (
                f"started in background (id {job_id}). output streams to "
                f"{log_path.relative_to(self.root).as_posix()}. "
                "You will not be notified automatically; do not poll."
            )
        if action == "install":
            with self.install_lock:
                result = run_shell(validated.command, self.env.shell, cwd, timeout)
        else:
            result = run_shell(validated.command, self.env.shell, cwd, timeout)
        if before is not None:
            after = _fingerprint(self.root)
            leaked = [
                path
                for path, digest in after.items()
                if before.get(path) != digest and not self._owned_by(agent_id, path)
            ]
            if leaked:
                return (
                    result.render(timeout)
                    + "\n[warning: this command changed files outside your lease: "
                    + ", ".join(leaked[:12])
                    + "]"
                )
        return result.render(timeout)

    def _owned_by(self, agent_id: str, path: str) -> bool:
        try:
            self.leases.check_write(agent_id, path)
            return True
        except LeaseError:
            return False

    # ── questions and web ───────────────────────────────────────────────────

    def _ask_user(self, tool_input, agent, call_id, notify) -> str:
        validated = tool_schemas.AskUser.model_validate(tool_input)
        agent_id, label, _, _, role_rules = self._agent_bits(agent)
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action="question",
            resource="*",
            role_rules=role_rules,
            call_id=call_id,
            notify=notify,
        )
        from kiln.companion.protocol import QuestionPayload

        questions = [
            Question(q.header, q.question, list(q.options), q.multiple) for q in validated.questions
        ]
        if notify is not None:
            notify(
                ToolProgress(
                    id=call_id or "ask",
                    state="awaiting_approval",
                    agent=agent_id,
                    action="question",
                    detail=f"{label} is waiting for your answers",
                    questions=[
                        QuestionPayload(
                            header=q.header,
                            question=q.question,
                            options=list(q.options),
                            multiple=q.multiple,
                        )
                        for q in validated.questions
                    ],
                )
            )
        answers = self.prompt.ask_questions(label, questions)
        if notify is not None:
            notify(
                ToolProgress(
                    id=call_id or "ask",
                    state="approval_resolved",
                    agent=agent_id,
                    action="question",
                    approved=True,
                )
            )
        lines = []
        for question, answer in zip(validated.questions, answers, strict=True):
            text = ", ".join(answer) if answer else "(skipped)"
            lines.append(f'"{question.question}"="{text}"')
        return "User answered: " + "; ".join(lines)

    def _web_fetch(self, tool_input, agent, call_id, notify) -> str:
        validated = tool_schemas.WebFetch.model_validate(tool_input)
        agent_id, label, _, _, role_rules = self._agent_bits(agent)
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action="web_fetch",
            resource=validated.url,
            role_rules=role_rules,
            request=ApprovalRequest(
                agent_label=label,
                action="web_fetch",
                lines=[validated.url],
                note="fetches a web page over the network",
                allow_run_scope=False,
            ),
            call_id=call_id,
            notify=notify,
        )
        return fetch_url(validated.url)

    def _web_search(self, tool_input, agent, call_id, notify) -> str:
        validated = tool_schemas.WebSearch.model_validate(tool_input)
        agent_id, label, _, _, role_rules = self._agent_bits(agent)
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action="web_search",
            resource=validated.query,
            role_rules=role_rules,
            request=ApprovalRequest(
                agent_label=label,
                action="web_search",
                lines=[validated.query],
                note="searches the web",
                allow_run_scope=True,
            ),
            remember_key="web_search",
            call_id=call_id,
            notify=notify,
        )
        return search_web(validated.query, provider=self.settings.web_search_provider)

    # ── stack / tests ───────────────────────────────────────────────────────

    def _probe_stack(self, tool_input, agent, call_id, notify) -> str:
        stack_id = str(tool_input.get("stack_id") or "python")
        profile = profile_for(stack_id)
        agent_id, label, _, _, _ = self._agent_bits(agent)
        probes = []
        for need in profile.needs:
            for argv in need.probes(self.env.os):
                probes.append(" ".join(argv))
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action="probe",
            resource=stack_id,
            request=ApprovalRequest(
                agent_label=label,
                action="probe",
                lines=probes or ["(no probes)"],
                shell=self.env.shell.label,
                note=f"check whether {profile.label} is installed (read-only version commands)",
                allow_run_scope=True,
            ),
            remember_key=f"probe:{stack_id}",
            call_id=call_id,
            notify=notify,
        )
        report: dict = {"stack_id": stack_id, "label": profile.label, "tools": {}, "ready": True}
        python_argv = None
        for need in profile.needs:
            found = None
            version_text = ""
            for argv in need.probes(self.env.os):
                result = run_argv(list(argv), self.root, timeout=30)
                if result.ok:
                    found = list(argv)
                    version_text = result.output.strip().splitlines()[0] if result.output.strip() else ""
                    break
            version = parse_version(version_text)
            ok = bool(found) and (version is None or version >= need.min_version)
            report["tools"][need.key] = {
                "label": need.label,
                "found": found,
                "version": version_text,
                "ok": ok,
                "installed_with": need.installed_with,
                "install": install_command(need.key, self.env.os, self.env.tools),
            }
            if not ok and not need.installed_with:
                report["ready"] = False
            if need.key == "python" and found:
                # probes end with --version / -V; keep the interpreter argv (e.g. ["py", "-3"])
                python_argv = (
                    found[:-1] if found[-1] in ("--version", "-V", "version") else found
                )
        self.stack_id = stack_id
        self._python_argv = python_argv
        return json.dumps(report)

    def _install_tool(self, tool_input, agent, call_id, notify) -> str:
        tool_key = str(tool_input.get("tool") or "")
        argv = install_command(tool_key, self.env.os, self.env.tools)
        if not argv:
            raise ExecutionError(
                f"no package manager on this machine can install {tool_key!r}; "
                "install it yourself and re-run, or pick another stack"
            )
        agent_id, label, _, _, _ = self._agent_bits(agent)
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action="install",
            resource=" ".join(argv),
            request=ApprovalRequest(
                agent_label=label,
                action="install",
                lines=[" ".join(argv)],
                shell=self.env.shell.label,
                note=f"install {tool_key} on this computer",
                allow_run_scope=False,
            ),
            call_id=call_id,
            notify=notify,
        )
        with self.install_lock:
            result = run_argv(argv, self.root, timeout=600)
        refresh_windows_path()
        self.env = detect_environment()
        if not result.ok:
            raise ExecutionError(result.render(600))
        return f"installed {tool_key}\n{result.render(600)}"

    def _install_deps(self, tool_input, agent, call_id, notify) -> str:
        stack_id = str(tool_input.get("stack_id") or self.stack_id)
        profile = profile_for(stack_id)
        commands = profile.install_deps_commands(self.root, self.env.os, self._python_argv)
        if not commands:
            return f"nothing to install for {stack_id}"
        agent_id, label, _, _, _ = self._agent_bits(agent)
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action="install",
            resource=f"deps:{stack_id}",
            request=ApprovalRequest(
                agent_label=label,
                action="install",
                lines=[" ".join(cmd) for cmd in commands],
                shell=self.env.shell.label,
                note="install the project's dependencies into the workspace",
                allow_run_scope=False,
            ),
            call_id=call_id,
            notify=notify,
        )
        outputs = []
        with self.install_lock:
            for cmd in commands:
                result = run_argv(cmd, self.root, timeout=600)
                outputs.append(result.render(600))
                if not result.ok:
                    raise ExecutionError("\n".join(outputs))
        self.stack_id = stack_id
        return "\n".join(outputs)

    def _run_tests(self, tool_input, agent, call_id, notify) -> str:
        stack_id = str(tool_input.get("stack_id") or self.stack_id)
        if self.settings.sandbox == "docker":
            report = run_tests_docker(self.root, self.settings)
            return report.model_dump_json()
        profile = profile_for(stack_id)
        cmd = profile.test_command(self.root, self.env.os, self._python_argv)
        agent_id, label, _, _, _ = self._agent_bits(agent)
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action="shell",
            resource=" ".join(cmd),
            request=ApprovalRequest(
                agent_label=label,
                action="shell",
                lines=[" ".join(cmd)],
                shell=self.env.shell.label,
                note="run the project's tests",
                allow_run_scope=True,
            ),
            remember_key=" ".join(cmd),
            call_id=call_id,
            notify=notify,
        )
        result = run_argv(cmd, self.root, timeout=self.settings.sandbox_timeout_seconds)
        results = self.root / RESULTS_FILE
        if not results.is_file():
            raise ExecutionError(
                f"tests produced no {RESULTS_FILE}\n{result.render()}"
            )
        report = parse_junit(results)
        log = result.output
        return report.model_copy(update={"raw_log": report.raw_log or log}).model_dump_json()

    def _smoke_check(self, tool_input, agent, call_id, notify) -> str:
        stack_id = str(tool_input.get("stack_id") or self.stack_id)
        paths = list(tool_input.get("paths") or [])
        profile = profile_for(stack_id)
        commands = profile.smoke_commands(self.root, paths, self.env.os, self._python_argv)
        if not commands:
            return "no smoke checks for these paths"
        agent_id, label, _, _, _ = self._agent_bits(agent)
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action="shell",
            resource=f"smoke:{stack_id}",
            request=ApprovalRequest(
                agent_label=label,
                action="shell",
                lines=[" ".join(cmd) for cmd in commands],
                shell=self.env.shell.label,
                note="fast syntax/type check before the real tests",
                allow_run_scope=True,
            ),
            remember_key=f"smoke:{stack_id}",
            call_id=call_id,
            notify=notify,
        )
        parts = []
        for cmd in commands:
            result = run_argv(cmd, self.root, timeout=120)
            parts.append(result.render())
            if not result.ok:
                # a smoke command that ran and failed is a broken file, not
                # advice: the orchestrator fails the layer instead of building
                # on top of it
                raise ExecutionError("\n".join(parts))
        return "\n".join(parts) or "ok"

    def _served_ui_audit(self, tool_input, agent, call_id, notify) -> str:
        from kiln.product.served_ui_audit import audit_served_ui

        idea = str(tool_input.get("idea") or "")
        discovery = str(tool_input.get("discovery") or "")
        gaps = audit_served_ui(self.root, idea=idea, discovery=discovery)
        return json.dumps({"ok": not gaps, "gaps": gaps})

    def _runtime_smoke(self, tool_input, agent, call_id, notify) -> str:
        from kiln.product.runtime_smoke import run_runtime_smoke

        paths = list(tool_input.get("paths") or ["/"])
        agent_id, label, _, _, _ = self._agent_bits(agent)
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action="shell",
            resource="runtime_smoke",
            request=ApprovalRequest(
                agent_label=label,
                action="shell",
                lines=["start app briefly", "GET " + ", ".join(paths)],
                shell=self.env.shell.label,
                note="host HTTP smoke after the suite goes green",
                allow_run_scope=True,
            ),
            remember_key="runtime_smoke",
            call_id=call_id,
            notify=notify,
        )
        gaps = run_runtime_smoke(self.root, paths=paths)
        return json.dumps({"ok": not gaps, "gaps": gaps})

    def _browser_verify(self, tool_input, agent, call_id, notify) -> str:
        from kiln.product.browser_verify import run_browser_verify

        idea = str(tool_input.get("idea") or "")
        discovery = str(tool_input.get("discovery") or "")
        base_url = tool_input.get("base_url")
        agent_id, label, _, _, _ = self._agent_bits(agent)
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action="shell",
            resource="browser_verify",
            request=ApprovalRequest(
                agent_label=label,
                action="shell",
                lines=[
                    "playwright chromium click-through",
                    "docs/browser-flows.json or heuristic FlowPlan",
                ],
                shell=self.env.shell.label,
                note="host browser verify after runtime_smoke (UI products)",
                allow_run_scope=True,
            ),
            remember_key="browser_verify",
            call_id=call_id,
            notify=notify,
        )
        gaps = run_browser_verify(
            self.root,
            idea=idea,
            discovery=discovery,
            base_url=str(base_url) if base_url else None,
        )
        return json.dumps({"ok": not gaps, "gaps": gaps})

    def _claim_scope(self, tool_input, agent, call_id, notify) -> str:
        agent_id, label, _, owned_default, _ = self._agent_bits(agent)
        owned = list(tool_input.get("owned_paths") or owned_default)
        if not owned:
            raise ExecutionError("claim_scope needs owned_paths")
        self.leases.claim(agent_id, label, owned)
        peer = {
            "id": agent_id,
            "label": label,
            "owned_paths": owned,
            "status": str(tool_input.get("status") or "working"),
            "task": str(tool_input.get("task") or ""),
            "files": list(tool_input.get("files") or []),
        }
        with self._peers_lock:
            self._peers[agent_id] = peer
        return f"leased {', '.join(owned)} to {label}"

    def _release_scope(self, tool_input, agent, call_id, notify) -> str:
        agent_id, label, _, _, _ = self._agent_bits(agent)
        self.leases.release(agent_id)
        with self._peers_lock:
            self._peers.pop(agent_id, None)
        return f"released lease for {label}"

    def _list_peers(self, tool_input, agent, call_id, notify) -> str:
        with self._peers_lock:
            peers = list(self._peers.values())
        if not peers:
            return "(no other developers active)"
        return json.dumps(peers, indent=2)

    def _confirm_plan(self, tool_input, agent, call_id, notify) -> str:
        """The approve-to-proceed beat: the team never builds on an unreviewed
        plan. The plan summary is shown in this terminal; a refusal can carry
        feedback that goes back to the planner for one revision."""
        agent_id, label, _, _, _ = self._agent_bits(agent)
        lines = [str(line) for line in (tool_input.get("lines") or [])][:14]
        if not lines:
            lines = ["(the planner produced no summary)"]
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action="plan",
            resource="task-graph",
            request=ApprovalRequest(
                agent_label=label,
                action="review the plan and build",
                lines=lines,
                note="the team builds exactly what you approve here; "
                "deny with feedback to send the planner back once",
                allow_run_scope=False,
            ),
            call_id=call_id,
            notify=notify,
        )
        return "plan approved"

    def _vendor_deps_legacy(self, tool_input, agent, call_id, notify) -> str:
        """Kept for the optional Docker sandbox path only."""
        if self.settings.sandbox != "docker":
            return "vendor_deps is only used with the Docker sandbox; use install_deps on the host"
        # reuse the old manylinux download so Docker can stay offline
        import subprocess
        import sys

        requirements = self.root / "requirements.txt"
        if not requirements.is_file():
            return "no requirements.txt; nothing to vendor"
        target = self.root / "vendor"
        target.mkdir(exist_ok=True)
        completed = subprocess.run(
            [
                sys.executable, "-m", "pip", "download", "-q", "--dest", str(target),
                "-r", str(requirements), "--platform", "manylinux2014_x86_64",
                "--python-version", "312", "--implementation", "cp", "--abi", "cp312",
                "--only-binary=:all:",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[:400]
            raise ExecutionError(f"pip download failed: {detail}")
        wheels = len(list(target.glob("*.whl"))) + len(list(target.glob("*.tar.gz")))
        return f"vendored {wheels} packages into vendor/"
