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

from kiln.agents.briefing import briefing_reminder_suffix, product_briefing_blocks
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
    ui_product: bool = False,
    prod_ops: bool = False,
    security: bool = False,
) -> list[dict[str, str]]:
    user = (
        "Deliver your task. Answer with the worker result JSON only.\n\n"
        f"<task>\n{task.model_dump_json(indent=2)}\n</task>\n\n"
        f"<spec>\n{spec.model_dump_json(indent=2)}\n</spec>\n"
        + product_briefing_blocks(
            design=design, discovery=discovery, prefer_design=ui_product
        )
        + "\n"
        + _tests_block(suite)
        + _contract_block(contract)
        + _current_files_block(current_files or {})
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
            "content": system_prompt_for(Role.WORKER, ui_product=ui_product, prod_ops=prod_ops, security=security),
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
    ui_product: bool = False,
    prod_ops: bool = False,
    security: bool = False,
) -> WorkerResult:
    """One-shot structured delivery. One retry with the error shown."""
    try:
        return _attempt(
            gateway,
            task,
            _messages(
                task,
                spec,
                suite,
                current_files,
                None,
                contract,
                test_state,
                design=design,
                discovery=discovery,
                ui_product=ui_product,
                prod_ops=prod_ops,
                security=security,
            ),
        )
    except ValidationError as first:
        try:
            return _attempt(
                gateway,
                task,
                _messages(
                    task,
                    spec,
                    suite,
                    current_files,
                    str(first),
                    contract,
                    test_state,
                    design=design,
                    discovery=discovery,
                    ui_product=ui_product,
                    prod_ops=prod_ops,
                    security=security,
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
    ui_product: bool = False,
    prod_ops: bool = False,
    security: bool = False,
) -> WorkerResult:
    """Tool-using developer on the user's machine."""
    definition = definition_for(Role.WORKER)
    system = system_prompt_for(Role.WORKER, ui_product=ui_product, prod_ops=prod_ops, security=security)
    shell = _shell_prompt(environment)
    if shell:
        system = system + "\n\n" + shell
    briefing = product_briefing_blocks(
        design=design, discovery=discovery, prefer_design=ui_product
    )
    user = (
        "Deliver your task with the tools. When finished, call submit_result.\n\n"
        f"<task>\n{task.model_dump_json(indent=2)}\n</task>\n\n"
        f"<spec>\n{spec.model_dump_json(indent=2)}\n</spec>\n"
        + briefing
        + _contract_block(contract)
        + _env_block(environment, stack_notes)
        + "\n\n"
        + _tests_block(suite)
        + _current_files_block(current_files or {})
        + _dependency_paths_block(dependency_paths)
    )
    if test_state:
        user += "\n\n" + test_state

    def check(payload: SubmitDev) -> str | None:
        if payload.task_id != task.id:
            return f"task_id must be {task.id}"
        for path in payload.files_written:
            if path.startswith("tests/"):
                return "workers never write test files"
            if not any(path == p or path.startswith(p.rstrip("/") + "/") for p in task.owned_paths):
                return f"{path} is outside the task's owned paths"
        return None

    # re-asserted on doom-loop refusals and the step-cap handoff
    reminder = (
        f"your task owns {', '.join(task.owned_paths)}"
        + briefing_reminder_suffix(has_design=bool((design or "").strip()))
    )
    if contract is not None:
        reminder += "; code against this contract:\n" + contract.model_dump_json(indent=2)

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
        )
    except AgentLoopError as exc:
        raise WorkerError(str(exc)) from exc

    # Merge unfinished todos into known_gaps when the model omitted them.
    gaps = list(outcome.result.known_gaps or [])
    for gap in todo_store.open_as_gaps():
        if gap not in gaps:
            gaps.append(gap)

    files: list[WorkFile] = []
    for path in outcome.result.files_written:
        try:
            content = await call_tool("read_file", {"path": path})
        except Exception:
            continue
        if content.startswith("# lines "):
            # strip the range header the companion adds for partial reads
            content = content.split("\n", 1)[-1]
        if content:
            files.append(WorkFile(path=path, content=content))
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
