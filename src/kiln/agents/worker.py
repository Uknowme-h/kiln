"""Dev worker: one task, written only inside the paths that task owns.

Two modes:
- one-shot structured JSON (legacy / tests), returning a WorkerResult with file
  contents inline;
- agentic tool loop on the user's machine: the model writes through the
  companion, then calls submit_result; we re-read the files it named.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from kiln.agents.briefing import (
    briefing_reminder_suffix,
    dependency_handoff_block,
    plan_memo_block,
    product_briefing_blocks,
    spec_excerpt_for_task,
)
from kiln.agents.coding_tools import wrap_coding_tools
from kiln.agents.definitions import definition_for, system_prompt_for
from kiln.agents.loop import AgentLoopError, run_agent_loop
from kiln.agents.prompts import load_prompt
from kiln.agents.roles import Role
from kiln.agents.transcript import TranscriptLog
from kiln.config import Settings
from kiln.gateway.client import Gateway
from kiln.schemas.jsonschema import response_format
from kiln.schemas.spec import Spec
from kiln.schemas.suite import TestSuite
from kiln.schemas.tasks import Contract, Task
from kiln.schemas.work import WorkerResult, WorkFile
from kiln.tools.registry import specs_for
from kiln.tools.schemas import SubmitDev


def _coding_tool_names(names: tuple[str, ...], settings: Settings) -> tuple[str, ...]:
    drop: set[str] = set()
    if not settings.enable_explore_subagent:
        drop.add("spawn_explore")
    if not settings.enable_todo_tool:
        drop.update({"todo_write", "todo_read"})
    if not settings.post_edit_diagnostics:
        drop.add("diagnostics")
    return tuple(n for n in names if n not in drop)

CallTool = Callable[[str, dict], Awaitable[str]]

MAX_SUITE_CHARS = 6000
MAX_FILE_CHARS = 4000
MAX_GREEN_LISTED = 40


class WorkerError(RuntimeError):
    """The worker could not produce a valid result."""


def _in_scope(task: Task, path: str) -> bool:
    return not path.startswith("tests/") and any(
        path == p or path.startswith(p.rstrip("/") + "/") for p in task.owned_paths
    )


async def _read_back(call_tool: CallTool, paths: list[str]) -> list[WorkFile]:
    """The files as they are on disk now — the worker's real output."""
    files: list[WorkFile] = []
    for path in paths:
        try:
            content = await call_tool("read_file", {"path": path})
        except Exception:
            continue
        if content.startswith("# lines "):
            # strip the range header the companion adds for partial reads
            content = content.split("\n", 1)[-1]
        if content:
            files.append(WorkFile(path=path, content=content))
    return files


async def _existing_owned_files(call_tool: CallTool, task: Task) -> list[str]:
    """Which of the task's owned files already exist on disk (dirs expanded)."""
    found: list[str] = []
    for owned in task.owned_paths:
        if owned.startswith("tests/"):
            continue
        try:
            listing = await call_tool("glob", {"pattern": "**/*", "path": owned})
        except Exception:
            listing = ""
        hits = [
            line.strip()
            for line in (listing or "").splitlines()
            if line.strip() and line.strip() != "(no matches)"
        ]
        if hits:  # an owned directory
            found.extend(hit for hit in hits[:20] if hit not in found)
            continue
        try:  # an owned file: a ranged read of an existing file has a "# lines" header
            probe = await call_tool("read_file", {"path": owned, "limit": 1})
        except Exception:
            continue
        if probe.startswith("# lines") and owned not in found:
            found.append(owned)
    return found


async def _salvage(
    task: Task,
    exc: AgentLoopError,
    call_tool: CallTool,
    todo_store,
    transcript: TranscriptLog | None,
    agent_id: str | None,
) -> WorkerResult | None:
    """An honest partial result when the loop ended without a valid handoff.

    The files are already on disk and the suite, fixer and reviewer judge them
    anyway; throwing them away (and the whole run with them) because one
    submit_result call was malformed is the worst outcome. The result says
    plainly that it is unverified, so nothing downstream mistakes it for done.
    Nothing written in scope means nothing to salvage: the caller raises.
    """
    files = await _read_back(call_tool, [p for p in exc.written_paths if _in_scope(task, p)])
    if not files:
        return None
    gaps = [
        f"The worker's loop ended without a valid handoff ({exc}). The files listed were "
        "written but the task was not verified complete; check them against the suite "
        "and the contract."
    ]
    for gap in todo_store.open_as_gaps():
        if gap not in gaps:
            gaps.append(gap)
    if transcript is not None:
        transcript.record(
            agent_id or "worker",
            Role.WORKER.value,
            "note",
            {"text": "salvaged partial result", "reason": str(exc), "files": [f.path for f in files]},
        )
    return WorkerResult.model_validate(
        {
            "task_id": task.id,
            "summary": f"Partial delivery for {task.title}: {len(files)} file(s) written before "
            "the worker's loop ended without a handoff.",
            "files": [{"path": f.path, "content": f.content} for f in files],
            "what_works": "Unverified: the files are on disk, but the worker did not hand off a "
            "verified result.",
            "known_gaps": gaps,
        },
        context={"task_id": task.id, "owned_paths": task.owned_paths},
    )


def _tests_block(suite: TestSuite | None) -> str:
    if suite is None:
        return "<tests>\n(no suite provided)\n</tests>"
    parts = []
    for file in suite.files:
        content = file.content[:MAX_SUITE_CHARS]
        if len(file.content) > MAX_SUITE_CHARS:
            content += "\n# ... trimmed ..."
        parts.append(f"# {file.path}\n{content}")
    return "<tests>\n" + "\n\n".join(parts) + "\n</tests>"


def _tests_digest_block(suite: TestSuite | None, task: Task) -> str:
    """Paths + covered ACs, not bodies — the agentic worker reads exact files.

    The one-shot worker gets the bodies inline (it has no tools). The agentic
    worker has read_file; inlining every suite body on every turn is the
    prompt bloat behind run-9f836de2's 3.27M worker prompt tokens (worker-5
    alone: 942k). The digest keeps the "code against the suite" contract while
    the worker reads only the tests it actually needs.
    """
    if suite is None:
        return "<tests>\n(no suite provided)\n</tests>"
    task_covers = set(task.covers)
    lines = []
    for file in suite.files:
        covers = sorted(set(file.covers))
        relevant = (not task_covers) or bool(task_covers & set(file.covers))
        tag = "covers your task" if relevant else "read if a seam touches it"
        acs = ", ".join(covers) if covers else "none listed"
        lines.append(f"- {file.path} ({tag}; ACs: {acs})")
    return (
        "<tests>\n"
        "The suite was written from the spec before you started; you may never "
        "edit it or write under tests/. Read the exact files you need with "
        "read_file before you code against them — do not guess their assertions:\n"
        + "\n".join(lines)
        + "\n</tests>"
    )


def _contract_digest(contract: Contract) -> str:
    """One line per route + module + shared file: the reminder's compact form.

    The full pretty-printed contract JSON is fine once in the brief, but the
    drift reminder re-injects it on doom-loop refusal, step-cap, and after
    compaction — a digest re-asserts the seams without re-paying the JSON.
    """
    lines = [f"{route.method} {route.path}" for route in contract.routes]
    lines.extend(contract.modules)
    if contract.shared_files:
        lines.append("scaffold owns: " + ", ".join(contract.shared_files))
    return "\n".join(lines)


def _current_files_block(current_files: dict[str, str]) -> str:
    if not current_files:
        return ""
    blocks = []
    for path, content in current_files.items():
        trimmed = content[:MAX_FILE_CHARS]
        if len(content) > MAX_FILE_CHARS:
            trimmed += "\n# ... trimmed ..."
        blocks.append(f'<current_files path="{path}">\n{trimmed}\n</current_files>')
    return "\n\n" + "\n\n".join(blocks)


def _dependency_paths_block(paths: list[str] | None) -> str:
    if not paths:
        return ""
    listed = "\n".join(f"- {path}" for path in paths)
    return (
        "\n\n<dependency_files>\n"
        "Files tasks you depend on already wrote:\n"
        f"{listed}\n"
        "Their contents are not inlined — read any you need with read_file "
        "before you rely on or overwrite them.\n"
        "</dependency_files>"
    )


def test_state_block(
    known_green: list[str], known_broken: int, regressions: list[str]
) -> str:
    """What the suite proved after the previous layer, for the next layer.

    The suite covers the whole spec, so red mid-build is expected — this block
    separates "still landing" failures from regressions of already-green tests.
    """
    if not known_green and not known_broken and not regressions:
        return ""
    listed = ", ".join(known_green[:MAX_GREEN_LISTED])
    if len(known_green) > MAX_GREEN_LISTED:
        listed += f", … (+{len(known_green) - MAX_GREEN_LISTED} more)"
    lines = [
        "<test_state>",
        f"The suite after the previous layer: {len(known_green)} passing, "
        f"{known_broken} failing. Red is expected right now — the suite covers "
        "the whole spec and later tasks are still landing.",
        f"known_green: {listed or '(none yet — this is the first layer)'}",
        f"regressions: {', '.join(regressions) or 'none'} — a regression is a "
        "test that passed after an earlier layer and fails now; regressions are "
        "never expected. Keep every known_green test green.",
        "</test_state>",
    ]
    return "\n\n" + "\n\n".join(lines)


def _contract_block(contract: Contract | None) -> str:
    if contract is None:
        return ""
    return f"\n\n<contract>\n{contract.model_dump_json(indent=2)}\n</contract>"


def _env_block(environment: dict | None, stack_notes: str) -> str:
    parts = []
    if environment:
        os_name = environment.get("os", "?")
        shell = (environment.get("shell") or {}).get("label", "?")
        parts.append(f"Environment: {os_name}, shell: {shell}.")
    if stack_notes:
        parts.append(stack_notes)
    if not parts:
        return ""
    return "\n\n<environment>\n" + "\n".join(parts) + "\n</environment>"


def _shell_prompt(environment: dict | None) -> str:
    os_name = (environment or {}).get("os", "")
    try:
        if os_name == "Windows":
            return load_prompt("shell_windows")
        if os_name == "macOS":
            return load_prompt("shell_macos")
        return load_prompt("shell_posix")
    except FileNotFoundError:
        return ""


def _messages(
    task: Task,
    spec: Spec,
    suite: TestSuite | None,
    current_files: dict[str, str] | None,
    error: str | None,
    contract: Contract | None = None,
    test_state: str = "",
    *,
    design: str | None = None,
    discovery: str | None = None,
    plan_memo: str | None = None,
    handoffs: list[dict] | None = None,
    ui_product: bool = False,
    prod_ops: bool = False,
    security: bool = False,
    extra_skills: tuple[str, ...] = (),
) -> list[dict[str, str]]:
    user = (
        "Deliver your task. Answer with the worker result JSON only.\n\n"
        f"<task>\n{task.model_dump_json(indent=2)}\n</task>\n\n"
        f"<spec>\n{spec.model_dump_json(indent=2)}\n</spec>\n"
        + product_briefing_blocks(
            design=design, discovery=discovery, prefer_design=ui_product
        )
        + plan_memo_block(plan_memo)
        + "\n"
        + _tests_block(suite)
        + _contract_block(contract)
        + _current_files_block(current_files or {})
        + dependency_handoff_block(handoffs)
    )
    if test_state:
        user += "\n\n" + test_state
    if error is not None:
        user += (
            "\n\nYour previous reply failed validation with this error:\n"
            f"<validation_error>\n{error}\n</validation_error>\n"
            "Fix the reply so it validates. Common causes: a file outside the task's "
            "owned paths, a file under tests/, or a result for the wrong task id. "
            "Return corrected JSON only."
        )
    return [
        {
            "role": "system",
            "content": system_prompt_for(Role.WORKER, ui_product=ui_product, prod_ops=prod_ops, security=security, extra_skills=extra_skills),
        },
        {"role": "user", "content": user},
    ]


def _attempt(
    gateway: Gateway,
    task: Task,
    messages: list[dict[str, str]],
) -> WorkerResult:
    completion = gateway.complete(
        Role.WORKER,
        messages,
        max_tokens=definition_for(Role.WORKER).max_output_tokens,
        response_format=response_format("worker_result", WorkerResult),
    )
    return WorkerResult.model_validate_json(
        completion.content,
        context={"task_id": task.id, "owned_paths": task.owned_paths},
    )


def run(
    gateway: Gateway,
    task: Task,
    spec: Spec,
    suite: TestSuite | None = None,
    current_files: dict[str, str] | None = None,
    contract: Contract | None = None,
    *,
    test_state: str = "",
    design: str | None = None,
    discovery: str | None = None,
    plan_memo: str | None = None,
    handoffs: list[dict] | None = None,
    ui_product: bool = False,
    prod_ops: bool = False,
    security: bool = False,
    extra_skills: tuple[str, ...] = (),
) -> WorkerResult:
    """One-shot structured delivery. One retry with the error shown."""
    brief = dict(
        design=design,
        discovery=discovery,
        plan_memo=plan_memo,
        handoffs=handoffs,
        ui_product=ui_product,
        prod_ops=prod_ops,
        security=security,
        extra_skills=extra_skills,
    )
    try:
        return _attempt(
            gateway,
            task,
            _messages(task, spec, suite, current_files, None, contract, test_state, **brief),
        )
    except ValidationError as first:
        try:
            return _attempt(
                gateway,
                task,
                _messages(
                    task, spec, suite, current_files, str(first), contract, test_state, **brief
                ),
            )
        except ValidationError as second:
            raise WorkerError(
                f"worker produced an invalid result twice: {second}"
            ) from second


async def run_agentic(
    gateway: Gateway,
    task: Task,
    spec: Spec,
    call_tool: CallTool,
    suite: TestSuite | None = None,
    current_files: dict[str, str] | None = None,
    contract: Contract | None = None,
    environment: dict | None = None,
    stack_notes: str = "",
    transcript: TranscriptLog | None = None,
    agent_id: str | None = None,
    dependency_paths: list[str] | None = None,
    test_state: str = "",
    *,
    design: str | None = None,
    discovery: str | None = None,
    plan_memo: str | None = None,
    handoffs: list[dict] | None = None,
    retry_note: str = "",
    ui_product: bool = False,
    prod_ops: bool = False,
    security: bool = False,
    extra_skills: tuple[str, ...] = (),
) -> WorkerResult:
    """Tool-using developer on the user's machine.

    ``retry_note`` is set when an earlier worker on this task failed without
    delivering anything: it says why, so the fresh attempt avoids the same end.
    """
    definition = definition_for(Role.WORKER)
    system = system_prompt_for(Role.WORKER, ui_product=ui_product, prod_ops=prod_ops, security=security, extra_skills=extra_skills)
    shell = _shell_prompt(environment)
    if shell:
        system = system + "\n\n" + shell
    briefing = product_briefing_blocks(
        design=design, discovery=discovery, prefer_design=ui_product
    )
    user = (
        "Deliver your task with the tools. When finished, call submit_result.\n\n"
        f"<task>\n{task.model_dump_json(indent=2)}\n</task>\n"
        # Task-relevant ACs + file_plan, not the whole spec: the worker has its
        # task and tools; every user-story and unrelated criterion is per-turn cost.
        + spec_excerpt_for_task(spec, task)
        + briefing
        + plan_memo_block(plan_memo)
        + _contract_block(contract)
        + _env_block(environment, stack_notes)
        + "\n\n"
        + _tests_digest_block(suite, task)
        + _current_files_block(current_files or {})
        + _dependency_paths_block(dependency_paths)
        + dependency_handoff_block(handoffs)
    )
    if test_state:
        user += "\n\n" + test_state
    if retry_note:
        user += (
            "\n\n<previous_attempt>\n"
            f"A previous worker on this task ended without delivering: {retry_note}\n"
            "Write your files early and call submit_result before you run out of "
            "steps; record anything unfinished as known_gaps.\n"
            "</previous_attempt>"
        )

    # An empty files_written is a legitimate answer when the task's deliverable
    # is already on disk and verified (run-7f74e69c's worker-5 ran the targeted
    # test, saw it pass and said "no changes needed" — and the run died). It
    # is only an error when there is no deliverable at all, and then it is
    # rejected HERE, inside the loop, so the model can still do the work.
    verified_existing: list[str] = []

    async def check(payload: SubmitDev) -> str | None:
        if payload.task_id != task.id:
            return f"task_id must be {task.id}"
        for path in payload.files_written:
            if path.startswith("tests/"):
                return "workers never write test files"
            if not any(path == p or path.startswith(p.rstrip("/") + "/") for p in task.owned_paths):
                return f"{path} is outside the task's owned paths"
        if not payload.files_written:
            existing = await _existing_owned_files(call_tool, task)
            if not existing:
                return (
                    "files_written is empty and none of your owned files exist yet "
                    f"({', '.join(task.owned_paths)}). Write your task's files, then list "
                    "them in files_written."
                )
            verified_existing[:] = existing
        return None

    # re-asserted on doom-loop refusals and the step-cap handoff
    reminder = (
        f"your task owns {', '.join(task.owned_paths)}"
        + briefing_reminder_suffix(has_design=bool((design or "").strip()))
    )
    if contract is not None:
        reminder += "; code against this contract:\n" + _contract_digest(contract)

    settings = gateway.settings
    tool_names = _coding_tool_names(definition.tools, settings)
    wrapped, todo_store, _quota = wrap_coding_tools(
        call_tool,
        gateway=gateway,
        settings=settings,
        agent_id=agent_id or "worker",
        transcript=transcript,
    )
    try:
        outcome = await run_agent_loop(
            gateway=gateway,
            role=Role.WORKER,
            system=system,
            user=user,
            tools=specs_for(tool_names),
            call_tool=wrapped,
            finish=SubmitDev,
            max_steps=definition.max_steps,
            max_tokens=definition.max_output_tokens,
            check=check,
            transcript=transcript,
            agent_id=agent_id,
            reminder=reminder,
            todo_store=todo_store if settings.enable_todo_tool else None,
            todo_probe_every=settings.todo_probe_every,
            enable_compaction=settings.enable_compaction,
            compact_threshold_chars=settings.compact_threshold_chars,
            prune_protect_chars=settings.prune_protect_chars,
        )
    except AgentLoopError as exc:
        salvaged = await _salvage(task, exc, call_tool, todo_store, transcript, agent_id)
        if salvaged is None:
            raise WorkerError(str(exc)) from exc
        return salvaged

    # Merge unfinished todos into known_gaps when the model omitted them.
    gaps = list(outcome.result.known_gaps or [])
    for gap in todo_store.open_as_gaps():
        if gap not in gaps:
            gaps.append(gap)

    # no changes needed: the deliverable is the owned files already on disk
    files = await _read_back(call_tool, list(outcome.result.files_written) or verified_existing)
    if not files and outcome.result.files_written:
        raise WorkerError("submit_result named files that could not be read back from the workspace")
    if not files:
        # allow a scaffold that only touched manifests already present — still need one file
        raise WorkerError("submit_result must name at least one file you wrote")

    return WorkerResult.model_validate(
        {
            "task_id": outcome.result.task_id,
            "summary": outcome.result.summary,
            "files": [{"path": f.path, "content": f.content} for f in files],
            "what_works": outcome.result.what_works,
            "known_gaps": gaps,
        },
        context={"task_id": task.id, "owned_paths": task.owned_paths},
    )


def run_agentic_sync(*args, **kwargs) -> WorkerResult:
    return asyncio.run(run_agentic(*args, **kwargs))
