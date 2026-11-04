"""Tool execution inside one user-chosen directory.

This is the only code that touches the user's disk, runs shell commands, or
reaches the network on their behalf. Every path is re-resolved under the
workspace root. Writes are checked against path leases. Shell, install, and
web tools ask the user in this terminal before they run.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from kiln.agents.definitions import DEFINITIONS
from kiln.agents.roles import Role
from kiln.companion.commandscan import scan_command, write_targets
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
from kiln.companion.offload import maybe_offload_tool_output
from kiln.config import Settings
from kiln.paths import safe_relative_path
from kiln.permissions import Ruleset
from kiln.sandbox.parser import parse_junit
from kiln.sandbox.runner import run_tests as run_tests_docker
from kiln.stacks.profiles import (
    RESULTS_FILE,
    install_command,
    parse_version,
    plan_node_suite_manifest,
    profile_for,
    write_manifest,
)
from kiln.stacks.scaffold_recipes import (
    npm_package_name,
    recipe_for,
    scaffold_argv,
    scaffold_files,
)
from kiln.tools import schemas as tool_schemas

Notify = Callable[[ToolProgress], None]

# read_file returns at most this much per call (whole lines), comfortably under
# the agent loop's 20k clip so a read is never trimmed mid-file.
READ_MAX_CHARS = 16_000

# Re-exported so the client can catch refusals without importing permissions.
__all__ = ["ExecutionError", "Executor", "PermissionDenied", "looks_like_kiln_repo"]


class ExecutionError(RuntimeError):
    """A tool input was invalid or the execution failed."""


def _merge_tree(source: Path, target: Path, prefix: str = "") -> list[str]:
    """Move ``source``'s contents into ``target`` without overwriting anything.

    Entries missing from ``target`` are moved whole (a rename on one volume);
    folders present on both sides are merged; files present on both sides
    keep the target's copy. Returns the kept paths, relative to ``target``.
    """
    kept: list[str] = []
    for entry in sorted(source.iterdir(), key=lambda p: p.name):
        destination = target / entry.name
        rel = f"{prefix}{entry.name}"
        if not destination.exists():
            shutil.move(str(entry), str(destination))
        elif entry.is_dir() and destination.is_dir():
            kept.extend(_merge_tree(entry, destination, f"{rel}/"))
        else:
            kept.append(rel)
    return kept


def _role_rules(role: str) -> Ruleset:
    try:
        return DEFINITIONS[Role(role)].permissions
    except (KeyError, ValueError):
        return ()


# Folders the after-shell change check never walks, at any depth: installs,
# VCS data, build output and caches. A nested frontend/node_modules used to be
# hashed file by file on every shell call.
_FINGERPRINT_SKIP = frozenset(
    {
        "node_modules", ".git", ".venv", "venv", ".kiln", ".next", ".nuxt", "dist",
        "build", "out", "coverage", "__pycache__", ".pytest_cache", ".mypy_cache",
        ".ruff_cache", ".turbo", ".cache", "vendor", ".deps",
    }
)  # fmt: skip
_FINGERPRINT_MAX_FILES = 20_000
# Dependency manifests and lockfiles: an install command run by either
# developer may legitimately change them (adding a dependency).
_SHARED_MANIFESTS = frozenset(
    {
        "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
        "pnpm-lock.yaml", "requirements.txt", "pyproject.toml", "poetry.lock", "uv.lock",
    }
)  # fmt: skip


def _fingerprint(root: Path) -> dict[str, tuple[int, int]] | None:
    """(size, mtime_ns) of every source file under root, or None if too many.

    Stat signatures, not content hashes: the check runs around every shell
    command, and reading every file each time was the slow part.
    """
    out: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _FINGERPRINT_SKIP]
        base = Path(dirpath)
        for name in filenames:
            path = base / name
            try:
                stat = path.stat()
            except OSError:
                continue
            out[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
            if len(out) > _FINGERPRINT_MAX_FILES:
                return None
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
        # auto_approve covers ONLY shell/install-style approvals (see
        # PermissionEngine); ask_user questions and the plan beat stay manual.
        self.permissions = PermissionEngine(
            self.prompt, auto_approve=bool(getattr(self.settings, "auto_approve", False))
        )
        self.leases = LeaseTable()
        self.locks = KeyedLocks()
        self.reads = ReadLedger()
        self.install_lock = threading.Lock()
        self.stack_id = "python"
        self._python_argv: list[str] | None = None
        self._peers: dict[str, dict] = {}
        self._peers_lock = threading.Lock()
        # who last wrote each path through the file tools, and when — so a
        # shell command is not blamed for a teammate's concurrent tool write
        self._tool_writes: dict[str, tuple[str, float]] = {}
        # (agent, start, end) of recent shell commands, to tell when two
        # developers' commands overlapped
        self._shell_runs: list[tuple[str, float, float | None]] = []
        self._shell_lock = threading.Lock()
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
        self._restore_scope_state()
        return self.root

    # ── scope-state rehydration (survive a respawn mid-build) ───────────────
    # The workspace file is a cache of who may write where and what they last
    # read. A respawned companion reloads it so an in-scope write is not
    # refused as LeaseError / never-read. The server's claim replay on
    # re-attach remains the authority; this file just keeps the companion
    # consistent between attach and that replay.
    _SCOPE_STATE = ".kiln/scope-state.json"

    def _restore_scope_state(self) -> None:
        path = (self.root or Path()) / self._SCOPE_STATE
        if not self.root or not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.leases.restore(data.get("leases") or {})
        self.reads.restore(data.get("reads") or {})

    def _persist_scope_state(self) -> None:
        if self.root is None:
            return
        path = self.root / self._SCOPE_STATE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"leases": self.leases.snapshot(), "reads": self.reads.snapshot()},
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass  # best-effort cache; the server replay is the authority

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
        scope = answer.scope if answer.scope in ("once", "run", "all") else "once"
        if scope == "all":
            # "allow all": approve this one and auto-approve the rest of the
            # run. Same reach as --auto-approve — the plan and ask_user still ask.
            if not answer.approved:
                scope = "once"
            else:
                self.set_auto_approve(True)
                scope = "run"  # coerced to once if this command isn't rememberable
        return self.prompt.answer_remotely(
            Reply(bool(answer.approved), scope, str(answer.feedback or ""))
        )

    def set_auto_approve(self, enabled: bool) -> None:
        """Flip auto-approve for command approvals (from the web floor)."""
        self.permissions.auto_approve = bool(enabled)

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
            "scaffold_framework": self._scaffold_framework,
            "run_tests": self._run_tests,
            "smoke_check": self._smoke_check,
            "served_ui_audit": self._served_ui_audit,
            "runtime_smoke": self._runtime_smoke,
            "ensure_browser_runtime": self._ensure_browser_runtime,
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
        output = handler(tool_input, agent, call_id, notify)
        if not isinstance(output, str):
            return output
        if self.root is None:
            return output
        agent_id, _, _, _, _ = self._agent_bits(agent)
        return maybe_offload_tool_output(
            self.root,
            tool=tool,
            output=output,
            agent_id=agent_id,
            call_id=call_id or "",
            threshold=int(self.settings.tool_offload_chars),
        )

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
        relative, path = self._resolve(validated.path)
        if not path.is_file():
            # A peer that is actively leased this path may simply not have
            # written it yet (parallel workers). That is information, not an
            # error — run-9f836de2 burned worker steps crash-looping on reads
            # of files a peer was still producing. Return a soft signal so the
            # model can wait, stub, or record a gap instead of "recovering".
            owner = self.leases.owner_of(relative)
            if owner is not None and owner[0] != agent_id:
                return (
                    f"{relative} does not exist yet — it is owned by {owner[1]}, "
                    "who is still working. Do not retry in a loop: code against the "
                    "contract stub, check list_peers for what they have written, or "
                    "record a known_gap if you depend on it."
                )
            raise ExecutionError(f"no such file in the workspace: {validated.path}")
        text = path.read_text(encoding="utf-8")
        self.reads.record(agent_id, relative, text)
        self._persist_scope_state()
        lines = text.splitlines(keepends=True)
        ranged = validated.offset is not None or validated.limit is not None
        start = (validated.offset or 1) - 1 if ranged else 0
        end = start + validated.limit if (ranged and validated.limit) else len(lines)
        end = min(end, len(lines))
        chunk = "".join(lines[start:end])
        if len(chunk) > READ_MAX_CHARS:
            # Return whole lines up to the budget plus an exact continuation
            # point — never a mid-file trim and never an offload stub.
            used, cut = 0, start
            for index in range(start, end):
                size = len(lines[index])
                if used + size > READ_MAX_CHARS and index > start:
                    break
                used += size
                cut = index + 1
            return (
                f"# lines {start + 1}-{cut} of {len(lines)} "
                f"(more below — continue with offset={cut + 1})\n"
                + "".join(lines[start:cut])
            )
        if not ranged:
            return text
        return f"# lines {start + 1}-{end} of {len(lines)}\n{chunk}"

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
            # match the relative path or the bare name: `include: "page.tsx"`
            # against `app/page.tsx` used to miss, and the agent concluded its
            # own freshly written file was empty (run-9bbe61f0)
            if validated.include and not (
                fnmatch.fnmatch(rel, validated.include)
                or fnmatch.fnmatch(path.name, validated.include)
            ):
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
        self._note_peer_write(agent_id, relative)
        self._persist_scope_state()
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
        self._note_peer_write(agent_id, relative)
        self._persist_scope_state()
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
        workdir_rel = validated.workdir or "."
        _, cwd = self._resolve(workdir_rel)
        if not cwd.is_dir():
            raise ExecutionError(f"workdir does not exist: {workdir_rel}")
        # Checked before anyone is asked, and never skipped by auto-approve: a
        # shell write must respect the same boundaries as write_file.
        problem = self._shell_write_problem(validated.command, cwd, agent_id)
        if problem:
            raise self.permissions.deny(agent_id, "shell", validated.command, f"refused: {problem}")
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
        timeout = (validated.timeout_ms or DEFAULT_TIMEOUT_SECONDS * 1000) / 1000
        watched = bool(owned) or agent_id in self.leases.active_agents()
        before = _fingerprint(self.root) if watched else None
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
        started = time.monotonic()
        run = (agent_id, started, None)
        with self._shell_lock:
            self._shell_runs.append(run)
        try:
            if action == "install":
                with self.install_lock:
                    result = run_shell(validated.command, self.env.shell, cwd, timeout)
            else:
                result = run_shell(validated.command, self.env.shell, cwd, timeout)
        finally:
            ended = time.monotonic()
            with self._shell_lock:
                self._shell_runs.remove(run)
                self._shell_runs.append((agent_id, started, ended))
                del self._shell_runs[:-50]
        output = result.render(timeout)
        if before is None:
            return output
        after = _fingerprint(self.root)
        if after is None:
            return output
        leaked, manifests = self._attribute_changes(
            before, after, agent_id, started, ended, installs=action == "install"
        )
        if manifests:
            output += "\n[note: this install updated shared manifests: " + ", ".join(manifests) + "]"
        if leaked:
            output += (
                "\n[warning: this command changed files outside your lease: "
                + ", ".join(leaked[:12])
                + "]"
            )
        return output

    def _shell_write_problem(self, command: str, cwd: Path, agent_id: str) -> str:
        """Why a command's literal write targets are not allowed, or ''.

        A target outside the workspace is refused for everyone; a target leased
        to another agent is refused like write_file would refuse it. Targets
        the scanner cannot read literally are left to the approval prompt and
        the after-run check.
        """
        root = self._require_root().resolve()
        for raw in write_targets(command, self.env.shell.kind):
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = cwd / candidate
            resolved = Path(os.path.normpath(str(candidate)))
            try:
                relative = resolved.relative_to(root).as_posix()
            except ValueError:
                return f"it would write {raw}, which is outside the workspace"
            if relative in ("", "."):
                continue
            owner = self.leases.owner_of(relative)
            if owner is not None and owner[0] != agent_id:
                return (
                    f"it would write {relative}, which is leased to {owner[1]}; ask them "
                    "with notify_peer or record a known_gap instead"
                )
        return ""

    def _attribute_changes(
        self,
        before: dict[str, tuple[int, int]],
        after: dict[str, tuple[int, int]],
        agent_id: str,
        started: float,
        ended: float,
        *,
        installs: bool,
    ) -> tuple[list[str], list[str]]:
        """Changes this command made to files another agent owns, plus manifests.

        Not blamed on this command: files the other agent wrote through its own
        tools while it ran, files owned by an agent whose shell command ran at
        the same time (ambiguous — the owner may have changed them), unowned
        files (build output, caches), and dependency manifests changed by an
        install (any developer may add a dependency; reported as a note).
        """
        changed = sorted(
            path
            for path in set(before) | set(after)
            if before.get(path) != after.get(path)
        )
        with self._shell_lock:
            overlapping = {
                other
                for other, start, end in self._shell_runs
                if other != agent_id and start < ended and (end is None or end > started)
            }
            tool_writes = dict(self._tool_writes)
        leaked: list[str] = []
        manifests: list[str] = []
        for path in changed:
            if installs and path.rsplit("/", 1)[-1] in _SHARED_MANIFESTS:
                manifests.append(path)
                continue
            owner = self.leases.owner_of(path)
            if owner is None or owner[0] == agent_id:
                continue
            written = tool_writes.get(path)
            if written is not None and written[0] == owner[0] and written[1] >= started:
                continue
            if owner[0] in overlapping:
                continue
            leaked.append(path)
        return leaked, manifests

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
        answers = self.prompt.ask_questions(label, questions, timeout=validated.timeout_seconds)
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
        # multi-root: install at the named root (a node frontend/ or a python
        # backend/); default stays the workspace root for single-root apps
        rel_root, root_path = self._resolve(str(tool_input.get("root") or "."))
        # The suite runs at the workspace root. For a node suite that root must
        # be able to run vitest whatever the scaffold left there (or nothing).
        manifest_plan: dict | None = None
        manifest_notes: list[str] = []
        if stack_id == "node" and rel_root in ("", "."):
            try:
                manifest_plan, manifest_notes = plan_node_suite_manifest(
                    root_path, npm_package_name(self._require_root().name)
                )
            except ValueError as exc:
                raise ExecutionError(str(exc)) from exc
        commands = profile.install_deps_commands(
            root_path, self.env.os, self._python_argv, venv_root=self._require_root()
        )
        if manifest_notes and not commands:
            commands = [["npm", "install"]]
        if not commands:
            return f"nothing to install for {stack_id} at {rel_root}"
        agent_id, label, _, _, _ = self._agent_bits(agent)
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action="install",
            resource=f"deps:{stack_id}:{rel_root}",
            request=ApprovalRequest(
                agent_label=label,
                action="install",
                lines=[f"(in {rel_root}) package.json: {note}" for note in manifest_notes]
                + [f"(in {rel_root}) " + " ".join(cmd) for cmd in commands],
                shell=self.env.shell.label,
                note=f"install the project's dependencies into {rel_root}",
                allow_run_scope=False,
            ),
            call_id=call_id,
            notify=notify,
        )
        if manifest_notes and manifest_plan is not None:
            write_manifest(root_path, manifest_plan)
        outputs = [f"package.json: {note}" for note in manifest_notes]
        with self.install_lock:
            for cmd in commands:
                result = run_argv(cmd, root_path, timeout=600)
                outputs.append(result.render(600))
                if not result.ok:
                    raise ExecutionError("\n".join(outputs))
        if rel_root in ("", "."):
            self.stack_id = stack_id
        return "\n".join(outputs)

    def _scaffold_framework(self, tool_input, agent, call_id, notify) -> str:
        """Deterministically scaffold one workspace root from an allowlisted recipe.

        Agents never run a create CLI themselves. An ``argv`` recipe runs the
        pinned official command here (stdin closed, CI=1); a ``template``
        recipe writes its pinned files under the same lease checks as any
        other write. Refuses a root that already has the recipe's manifest
        unless ``options.overwrite`` is true.
        """
        self._require_root()  # fails clearly when no workspace is bound
        framework = str(tool_input.get("framework") or "").strip()
        raw_root = str(tool_input.get("root") or "").strip() or "."
        options = tool_input.get("options") if isinstance(tool_input.get("options"), dict) else {}
        overwrite = bool((options or {}).get("overwrite"))
        recipe = recipe_for(framework)  # raises ValueError on unknown framework
        rel_root, root_path = self._resolve(raw_root)
        manifest = root_path / recipe.manifest
        if manifest.exists() and not overwrite:
            raise ExecutionError(
                f"root {rel_root!r} is already scaffolded ({recipe.manifest} exists); "
                "refusing to overwrite"
            )
        agent_id, label, _, _, _ = self._agent_bits(agent)
        if recipe.kind == "argv":
            return self._scaffold_staged(
                framework, recipe, rel_root, root_path, options, agent_id, label, call_id, notify
            )
        # template recipe: the companion writes the pinned tree
        files = scaffold_files(framework, rel_root)
        for rel_path, content in files:
            _, candidate = self._resolve(rel_path)
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text(content, encoding="utf-8")
        written = ", ".join(path for path, _ in files)
        return f"scaffolded {framework} at {rel_root} ({len(files)} files: {written})"

    def _scaffold_staged(
        self, framework, recipe, rel_root, root_path, options, agent_id, label, call_id, notify
    ) -> str:
        """Run an official create CLI in a staging folder, then move its output in.

        Create CLIs name the package after their target folder and refuse a
        non-empty target. Running them on the root in place failed for any
        folder npm cannot name (``Notescalidraw``: capitals) and for any root
        that already holds Kiln's own files (``.kiln/``, ``docs/discovery.md``)
        — run-9bbe61f0. The staging child is a fresh, empty folder with an
        npm-valid name derived from the root's folder name, so the package
        name stays meaningful. Staging lives under the workspace's ``.kiln/`` so
        the move is a same-volume rename (node_modules is never copied).
        Files that already exist in the root are kept, never overwritten.
        """
        workspace = self._require_root()
        folder = root_path.name if rel_root not in ("", ".") else workspace.name
        package = npm_package_name(folder)
        argv = scaffold_argv(framework, package, options)
        staging = workspace / ".kiln" / "scaffold" / f"{framework}-{uuid.uuid4().hex[:8]}"
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action="install",
            resource=f"scaffold:{framework}",
            request=ApprovalRequest(
                agent_label=label,
                action="install",
                lines=[" ".join(argv)],
                shell=self.env.shell.label,
                note=f"{recipe.note} into {rel_root!r} (built in a staging folder, then moved in)",
                allow_run_scope=False,
            ),
            call_id=call_id,
            notify=notify,
        )
        staging.mkdir(parents=True, exist_ok=True)
        try:
            with self.install_lock:
                # stdin is already DEVNULL; CI/npm_config_yes kill any prompt
                result = run_argv(
                    argv,
                    staging,
                    timeout=600,
                    env=scrubbed_env(extra={"CI": "1", "npm_config_yes": "1"}),
                )
            refresh_windows_path()
            if not result.ok:
                raise ExecutionError(result.render(600))
            built = staging / package
            if not built.is_dir():
                raise ExecutionError(
                    f"{framework} scaffolder finished but produced no {package}/ folder\n"
                    + result.render(600)
                )
            root_path.mkdir(parents=True, exist_ok=True)
            kept = _merge_tree(built, root_path)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            try:
                staging.parent.rmdir()  # .kiln/scaffold, when no other staging is live
            except OSError:
                pass
        if not (root_path / recipe.manifest).exists():
            raise ExecutionError(
                f"{framework} scaffold moved into {rel_root!r} but {recipe.manifest} is missing"
            )
        note = f"\nkept existing (not overwritten): {', '.join(kept)}" if kept else ""
        return f"scaffolded {framework} at {rel_root} (package {package}){note}\n{result.render(600)}"

    def smoke_root(self, stack_id: str, root: str) -> str:
        """Compile/type-check a scaffolded secondary root (no real suite yet)."""
        profile = profile_for(stack_id)
        commands = profile.smoke_commands_for_root(
            self._require_root(), root, self.env.os, self._python_argv
        )
        outputs = []
        for cmd in commands:
            result = run_argv(cmd, self._require_root(), timeout=300)
            outputs.append(result.render(300))
            if not result.ok:
                raise ExecutionError("\n".join(outputs))
        return "\n".join(outputs) or f"nothing to smoke at {root!r}"

    def _run_tests(self, tool_input, agent, call_id, notify) -> str:
        stack_id = str(tool_input.get("stack_id") or self.stack_id)
        if self.settings.sandbox == "docker":
            report = run_tests_docker(self.root, self.settings)
            return report.model_dump_json()
        profile = profile_for(stack_id)
        if stack_id == "node" and not (self.root / "node_modules" / "vitest").is_dir():
            # without this, npx answers with "canceled due to missing packages"
            # and the run dies on noise that does not name the cause
            raise ExecutionError(
                "vitest is not installed at the workspace root, so the suite cannot "
                "run. install_deps (stack node, root .) adds it to package.json and "
                "installs it."
            )
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

    def _ensure_browser_runtime(self, tool_input, agent, call_id, notify) -> str:
        """Provision Playwright + Chromium for UI product gates (PRP Phase 5.1).

        Detection is read-only and free. Installation runs only when the user
        approves — a declined or failed install returns ready=False (the
        browser gate then reports BLOCKED_HOST_SETUP rather than silently pass).
        """
        from kiln.product.browser_runtime import ensure_browser_runtime

        agent_id, label, _, _, _ = self._agent_bits(agent)
        self.permissions.authorize(
            agent_id=agent_id,
            agent_label=label,
            action="install",
            resource="playwright chromium",
            request=ApprovalRequest(
                agent_label=label,
                action="install",
                lines=[
                    "pip install playwright (if missing)",
                    "playwright install chromium (if missing)",
                ],
                shell=self.env.shell.label,
                note="provision the pinned headless browser so the UI product "
                "gates can actually run; skipping leaves them BLOCKED",
                allow_run_scope=True,
            ),
            remember_key="ensure_browser_runtime",
            call_id=call_id,
            notify=notify,
        )
        with self.install_lock:
            status = ensure_browser_runtime(install=True)
        return json.dumps(
            {
                "ready": status.ready,
                "playwright_version": status.playwright_version,
                "chromium_version": status.chromium_version,
                "installed": status.installed,
                "note": status.note,
            }
        )

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
        task = str(tool_input.get("task") or "")
        with self._peers_lock:
            previous = self._peers.get(agent_id) or {}
            # re-claiming for the same task (a borrowed file added to the lease)
            # keeps the record of what this agent already wrote
            files = list(previous.get("files") or []) if previous.get("task") == task else []
            self._peers[agent_id] = {
                "id": agent_id,
                "label": label,
                "owned_paths": owned,
                "status": str(tool_input.get("status") or "working"),
                "task": task,
                "files": files + [f for f in tool_input.get("files") or [] if f not in files],
            }
        self._persist_scope_state()
        return f"leased {', '.join(owned)} to {label}"

    def _release_scope(self, tool_input, agent, call_id, notify) -> str:
        agent_id, label, _, _, _ = self._agent_bits(agent)
        self.leases.release(agent_id)
        with self._peers_lock:
            self._peers.pop(agent_id, None)
        self._persist_scope_state()
        return f"released lease for {label}"

    def _note_peer_write(self, agent_id: str, relative: str) -> None:
        """Keep list_peers honest: it promises the files each peer has written."""
        with self._shell_lock:
            self._tool_writes[relative] = (agent_id, time.monotonic())
        with self._peers_lock:
            peer = self._peers.get(agent_id)
            if peer is not None:
                files = peer.setdefault("files", [])
                if relative not in files:
                    files.append(relative)

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
        lines = [str(line) for line in (tool_input.get("lines") or [])][:40]
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
