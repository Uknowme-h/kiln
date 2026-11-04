"""Fixer: a trimmed failing report and the owned code, returned as a patch.

Two modes:
- one-shot structured JSON (`run`) — legacy / non-agentic;
- agentic (`run_agentic`) — FIX_TOOLS on the host, finish via submit_result.

Validation always requires the patch only touches the failing task's owned
paths. The retry cap lives with the runner; the final attempt may escalate
to the strongest configured route.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from kiln.agents.briefing import (
    briefing_reminder_suffix,
    plan_memo_block,
    product_briefing_blocks,
    spec_excerpt_for_task,
)
from kiln.agents.coding_tools import wrap_coding_tools
from kiln.agents.definitions import definition_for, system_prompt_for
from kiln.agents.loop import AgentLoopError, run_agent_loop
from kiln.agents.roles import Role
from kiln.agents.transcript import TranscriptLog
from kiln.agents.worker import _coding_tool_names
from kiln.gateway.client import Gateway
from kiln.gateway.routing import strongest_route
from kiln.schemas.jsonschema import response_format
from kiln.schemas.patch import FixPatch
from kiln.schemas.results import TestReport
from kiln.schemas.spec import Spec
from kiln.schemas.tasks import Contract, Task
from kiln.schemas.work import WorkFile
from kiln.stacks.profiles import RESULTS_FILE
from kiln.tools.registry import FIX_SUBMIT, specs_for
from kiln.tools.schemas import SubmitFix

CallTool = Callable[[str, dict], Awaitable[str]]

MAX_LOG_CHARS = 6000
MAX_FILE_CHARS = 4000


class FixerError(RuntimeError):
    """The fixer could not produce a valid patch."""


def _summary_line(report: TestReport) -> str:
    """Aggregates first: the model sees the scoreline before the detail."""
    return (
        f"{report.passed} passed, {report.failed} failed, "
        f"{report.errors} errors, {report.skipped} skipped"
    )


def _trimmed_cases(report: TestReport) -> str:
    failing = [case for case in report.cases if case.outcome in ("failed", "error")]
    lines = []
    for case in failing[:10]:
        message = case.message[:400] if case.message else ""
        lines.append(f"- {case.nodeid} [{case.outcome}] {message}")
    return "\n".join(lines) or "(no per-case detail)"


def _current_files_block(current_files: dict[str, str]) -> str:
    blocks = []
    for path, content in current_files.items():
        trimmed = content[:MAX_FILE_CHARS]
        if len(content) > MAX_FILE_CHARS:
            trimmed += "\n# ... trimmed ..."
        blocks.append(f'<current_files path="{path}">\n{trimmed}\n</current_files>')
    return "\n\n".join(blocks)


def _file_index_block(paths: list[str]) -> str:
    """Owned paths to read, not their bodies — the agentic fixer must read_file
    before it may edit anyway (the companion's read-before-write ledger), so
    inlining every blamed file paid for each body twice."""
    listed = "\n".join(f"- {path}" for path in paths)
    return (
        "<owned_files>\n"
        "The files in scope for this fix. Read the ones the failures point at "
        "with read_file before you edit them:\n"
        f"{listed}\n"
        "</owned_files>"
    )


def _skill_kwargs(*, ui_product: bool = False, prod_ops: bool = False, security: bool = False, extra_skills: tuple[str, ...] = ()) -> dict:
    return {"ui_product": ui_product, "prod_ops": prod_ops, "security": security, "extra_skills": extra_skills}


def _messages(
    task: Task,
    report: TestReport,
    current_files: dict[str, str],
    error: str | None,
    contract: Contract | None = None,
    history: tuple[str, ...] | list[str] = (),
    known_gaps: tuple[str, ...] | list[str] = (),
    *,
    spec: Spec | None = None,
    design: str | None = None,
    discovery: str | None = None,
    plan_memo: str | None = None,
    ui_product: bool = False,
    prod_ops: bool = False,
    security: bool = False,
    extra_skills: tuple[str, ...] = (),
) -> list[dict[str, str]]:
    log = report.raw_log[:MAX_LOG_CHARS]
    contract_block = (
        f"\n\n<contract>\n{contract.model_dump_json(indent=2)}\n</contract>"
        if contract is not None
        else ""
    )
    excerpt = spec_excerpt_for_task(spec, task) if spec is not None else ""
    briefing = product_briefing_blocks(design=design, discovery=discovery) + plan_memo_block(
        plan_memo
    )
    user = (
        "Patch the code so these tests pass. Answer with the patch JSON only; "
        "each file in it carries the full corrected contents.\n\n"
        f"<task>\n{task.model_dump_json(indent=2)}\n</task>\n"
        + excerpt
        + briefing
        + "\n\n"
        f"<test_report>\n{_summary_line(report)}\n\n"
        f"{_trimmed_cases(report)}\n\nraw log (trimmed):\n{log}\n</test_report>"
        + contract_block
        + "\n\n"
        + _current_files_block(current_files)
    )
    if known_gaps:
        user += (
            "\n\n<known_gaps>\n"
            + "\n".join(f"- {gap}" for gap in known_gaps)
            + "\n</known_gaps>"
        )
    if history:
        user += (
            "\n\n<previous_attempts>\n"
            + "\n".join(f"- {diagnosis}" for diagnosis in history)
            + "\n</previous_attempts>\n"
            "An earlier patch with that diagnosis did not green the suite — do "
            "not repeat it; find what it missed."
        )
    if error is not None:
        user += (
            "\n\nYour previous reply failed validation with this error:\n"
            f"<validation_error>\n{error}\n</validation_error>\n"
            "Fix the reply so it validates. Common causes: a file outside the task's "
            "owned paths, or an empty diagnosis. Return corrected JSON only."
        )
    return [
        {
            "role": "system",
            "content": system_prompt_for(
                Role.FIXER,
                **_skill_kwargs(
                    ui_product=ui_product, prod_ops=prod_ops, security=security, extra_skills=extra_skills
                ),
            ),
        },
        {"role": "user", "content": user},
    ]


def _attempt(
    gateway: Gateway,
    task: Task,
    messages: list[dict[str, str]],
    escalate: bool = False,
) -> FixPatch:
    completion = gateway.complete(
        Role.FIXER,
        messages,
        max_tokens=definition_for(Role.FIXER).max_output_tokens,
        response_format=response_format("fix_patch", FixPatch),
        # escalation leaves the open-weight coding route for the meta/Spark
        # route the planning roles use
        route=strongest_route(gateway.settings) if escalate else None,
    )
    return FixPatch.model_validate_json(
        completion.content, context={"owned_paths": task.owned_paths}
    )


def run(
    gateway: Gateway,
    task: Task,
    report: TestReport,
    current_files: dict[str, str],
    contract: Contract | None = None,
    *,
    history: tuple[str, ...] | list[str] = (),
    known_gaps: tuple[str, ...] | list[str] = (),
    escalate: bool = False,
    spec: Spec | None = None,
    design: str | None = None,
    discovery: str | None = None,
    plan_memo: str | None = None,
    ui_product: bool = False,
    prod_ops: bool = False,
    security: bool = False,
    extra_skills: tuple[str, ...] = (),
) -> FixPatch:
    """Turn one failing report into a validated patch. One retry with the error shown.

    `history` is what earlier patches diagnosed (so a repeat diagnosis cannot
    be re-proposed blindly), `known_gaps` what the blamed workers admitted was
    unfinished, and `escalate` moves this call to the strongest configured
    route — used for the final attempt, when the run ends if this patch fails.
    """
    if not current_files:
        raise FixerError("the fixer needs the current contents of the task's files")
    skill = dict(
        plan_memo=plan_memo,
        ui_product=ui_product,
        prod_ops=prod_ops,
        security=security,
        extra_skills=extra_skills,
    )
    try:
        return _attempt(
            gateway,
            task,
            _messages(
                task,
                report,
                current_files,
                None,
                contract,
                history,
                known_gaps,
                spec=spec,
                design=design,
                discovery=discovery,
                **skill,
            ),
            escalate,
        )
    except ValidationError as first:
        try:
            return _attempt(
                gateway,
                task,
                _messages(
                    task,
                    report,
                    current_files,
                    str(first),
                    contract,
                    history,
                    known_gaps,
                    spec=spec,
                    design=design,
                    discovery=discovery,
                    **skill,
                ),
                escalate,
            )
        except ValidationError as second:
            raise FixerError(
                f"fixer produced an invalid patch twice: {second}"
            ) from second


async def run_agentic(
    gateway: Gateway,
    task: Task,
    report: TestReport,
    call_tool: CallTool,
    current_files: dict[str, str] | None = None,
    contract: Contract | None = None,
    *,
    history: tuple[str, ...] | list[str] = (),
    known_gaps: tuple[str, ...] | list[str] = (),
    escalate: bool = False,
    transcript: TranscriptLog | None = None,
    agent_id: str | None = None,
    spec: Spec | None = None,
    design: str | None = None,
    discovery: str | None = None,
    plan_memo: str | None = None,
    file_index: list[str] | None = None,
    ui_product: bool = False,
    prod_ops: bool = False,
    security: bool = False,
    extra_skills: tuple[str, ...] = (),
) -> FixPatch:
    """Tool-using fixer: read the JUnit log and owned files, then patch on disk.

    `known_gaps` leads with brief-review gaps when the suite is already green
    but the app does not deliver the brief. Those are the work — an empty
    patch is only acceptable when none of them is a code defect.

    `file_index` lists the in-scope paths to read instead of inlining bodies;
    `current_files` (inline bodies) is still honoured when a caller passes it.
    """
    definition = definition_for(Role.FIXER)
    system = system_prompt_for(
        Role.FIXER,
        **_skill_kwargs(
            ui_product=ui_product, prod_ops=prod_ops, security=security, extra_skills=extra_skills
        ),
    )
    if escalate:
        system += (
            "\n\nThis is the final allowed fix attempt — prefer a decisive, "
            "minimal patch over another partial try."
        )
    # Brief gaps reopen the fixer on a green suite. Frame that run around the
    # gaps, not the (already-green) JUnit, or the model reads the green report
    # and stands down — run-9f836de2 shipped an unmounted app exactly this way.
    green_suite_brief_work = report.green and bool(known_gaps)
    if green_suite_brief_work:
        opening = (
            "The suite is already green, but the app does not yet deliver the "
            "brief. The evidence-backed gaps in <known_gaps> below are the work: "
            "patch the code inside the task's owned paths so each one is resolved. "
            "Read the owned source files, find why each gap is true, and fix it. "
            "Never touch tests/. When every gap is addressed (or you can show it "
            "is host/tooling, not a code defect), call submit_result.\n\n"
        )
    else:
        opening = (
            "The suite is failing. Use your tools to read the JUnit report at "
            f"`{RESULTS_FILE}` and the owned source files, then edit or write fixes "
            "inside the task's owned paths only. Never touch tests/. When the "
            "failures are addressed (or you can go no further), call submit_result.\n\n"
        )
    excerpt = spec_excerpt_for_task(spec, task) if spec is not None else ""
    briefing = product_briefing_blocks(design=design, discovery=discovery) + plan_memo_block(
        plan_memo
    )
    user = (
        opening
        + f"<task>\n{task.model_dump_json(indent=2)}\n</task>\n"
        + excerpt
        + briefing
        + "\n\n"
        f"<test_report>\n{_summary_line(report)}\n\n{_trimmed_cases(report)}\n"
        f"</test_report>\n"
        f"<junit_path>{RESULTS_FILE}</junit_path>\n"
    )
    if contract is not None:
        user += f"\n<contract>\n{contract.model_dump_json(indent=2)}\n</contract>\n"
    if current_files:
        user += "\n" + _current_files_block(current_files)
    elif file_index:
        user += "\n" + _file_index_block(file_index)
    if known_gaps:
        user += (
            "\n\n<known_gaps>\n"
            + "\n".join(f"- {gap}" for gap in known_gaps)
            + "\n</known_gaps>"
        )
    if history:
        user += (
            "\n\n<previous_attempts>\n"
            + "\n".join(f"- {diagnosis}" for diagnosis in history)
            + "\n</previous_attempts>\n"
            "Do not repeat a diagnosis that already failed — find what it missed."
        )

    def check(payload: SubmitFix) -> str | None:
        if not payload.diagnosis.strip():
            return "diagnosis must not be empty"
        for path in payload.files_written:
            if path.startswith("tests/"):
                return "the fixer never writes test files"
            if not any(
                path == p or path.startswith(p.rstrip("/") + "/") for p in task.owned_paths
            ):
                return f"{path} is outside the task's owned paths"
        if green_suite_brief_work and not payload.files_written:
            return (
                "the brief reviewer filed code gaps but files_written is empty. "
                "Patch the files behind each gap, or explain in diagnosis why a "
                "gap is host/tooling (not a code defect) — an empty patch does "
                "not resolve a code gap."
            )
        return None

    settings = gateway.settings
    tool_names = _coding_tool_names(definition.tools, settings)
    tools = specs_for(tool_names, overrides={"submit_result": FIX_SUBMIT})
    wrapped, todo_store, _quota = wrap_coding_tools(
        call_tool,
        gateway=gateway,
        settings=settings,
        agent_id=agent_id or "fixer",
        transcript=transcript,
    )
    reminder = (
        f"owned paths: {', '.join(task.owned_paths)}"
        + briefing_reminder_suffix(has_design=bool((design or "").strip()))
    )
    try:
        outcome = await run_agent_loop(
            gateway=gateway,
            role=Role.FIXER,
            system=system,
            user=user,
            tools=tools,
            call_tool=wrapped,
            finish=SubmitFix,
            max_steps=definition.max_steps,
            max_tokens=definition.max_output_tokens,
            check=check,
            transcript=transcript,
            agent_id=agent_id or "fixer",
            reminder=reminder,
            route=strongest_route(gateway.settings) if escalate else None,
            todo_store=todo_store if settings.enable_todo_tool else None,
            todo_probe_every=settings.todo_probe_every,
            enable_compaction=settings.enable_compaction,
            compact_threshold_chars=settings.compact_threshold_chars,
            prune_protect_chars=settings.prune_protect_chars,
        )
    except AgentLoopError as exc:
        # Salvage a patch that is already on disk instead of aborting the run.
        # Only files really written in scope count — an empty patch is never
        # blessed (the brief-gap guard above stays intact). The re-run suite and
        # the bounded re-review still judge it; the diagnosis says it is partial.
        written = [
            p for p in exc.written_paths
            if not p.startswith("tests/")
            and any(p == o or p.startswith(o.rstrip("/") + "/") for o in task.owned_paths)
        ]
        salvaged: list[WorkFile] = []
        for path in written:
            try:
                content = await wrapped("read_file", {"path": path})
            except Exception:
                continue
            if content.startswith("# lines "):
                content = content.split("\n", 1)[-1]
            if content:
                salvaged.append(WorkFile(path=path, content=content))
        if not salvaged:
            raise FixerError(str(exc)) from exc
        return FixPatch.model_validate(
            {
                "diagnosis": f"(partial, unverified) the fixer's loop ended without a "
                f"handoff ({exc}); patched {', '.join(f.path for f in salvaged)}",
                "files": [{"path": f.path, "content": f.content} for f in salvaged],
            },
            context={"owned_paths": task.owned_paths},
        )

    files: list[WorkFile] = []
    for path in outcome.result.files_written:
        try:
            content = await wrapped("read_file", {"path": path})
        except Exception:
            continue
        if content.startswith("# lines "):
            content = content.split("\n", 1)[-1]
        if content:
            files.append(WorkFile(path=path, content=content))
    if not files:
        # Suite already green and the model found nothing to patch (e.g. a
        # product-gate that was really host env). Empty patch lets the
        # orchestrator exit the fix loop instead of aborting the run — but
        # only when there is no actionable brief work. With code gaps on the
        # table an empty patch is a no-op, not a resolution.
        if report.green and not outcome.result.files_written and not green_suite_brief_work:
            return FixPatch.model_validate(
                {
                    "diagnosis": outcome.result.diagnosis
                    or "no code changes; suite already green",
                    "files": [],
                },
                context={"owned_paths": task.owned_paths},
            )
        raise FixerError("submit_result must name at least one file you fixed")

    return FixPatch.model_validate(
        {
            "diagnosis": outcome.result.diagnosis,
            "files": [{"path": f.path, "content": f.content} for f in files],
        },
        context={"owned_paths": task.owned_paths},
    )
