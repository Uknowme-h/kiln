"""Declarative definition of every role on the floor.

Model routing stays in `gateway/routing.py`; prompts live in `agents/prompts/`.
Everything else about a role is data here: which tools it may call, which
workspace prefixes it may write by default, the permission rules the companion
enforces for it, and its caps. Workers and the fixer also receive per-task
owned paths at assignment time; those are evaluated on top of these defaults,
not instead of them.
"""

from dataclasses import dataclass

from kiln.agents.prompts import load_prompt
from kiln.agents.roles import Role
from kiln.agents.skills import (
    PROD_OPS_ROLES,
    SECURITY_ROLES,
    UI_PRODUCT_ROLES,
    load_skill,
    skill_index_block,
)
from kiln.permissions import Ruleset, role_rules

# What a tool-using developer may reach for. Kept small on purpose: a long tool
# list makes tool calling less reliable.
DEV_TOOLS = (
    "read_file",
    "list_dir",
    "glob",
    "grep",
    "write_file",
    "edit_file",
    "shell",
    "web_fetch",
    "web_search",
    "list_peers",
    "notify_peer",
    "ask_peer",
    "request_files",
    "propose_contract_change",
    "escalate",
    "skill",
    "spawn_explore",
    "todo_write",
    "todo_read",
    "diagnostics",
    "submit_result",
)
FIX_TOOLS = (
    "read_file",
    "list_dir",
    "glob",
    "grep",
    "write_file",
    "edit_file",
    "shell",
    "list_peers",
    "escalate",
    "skill",
    "spawn_explore",
    "todo_write",
    "todo_read",
    "diagnostics",
    "submit_result",
)


@dataclass(frozen=True)
class RoleDefinition:
    role: Role
    label: str
    color: str
    prompt: str
    tools: tuple[str, ...] = ()
    write_prefixes: tuple[str, ...] = ()
    permissions: Ruleset = ()
    max_steps: int = 20
    max_fix_attempts: int = 0
    # Optional per-role ceiling for one model call. None (the default for every
    # role) uses the generous safety net in config — Muse Spark reasons before it
    # answers, and a tight cap starves the answer, so length is guided in the
    # system prompt (output_guidance) instead of pinned here.
    max_output_tokens: int | None = None
    # The soft cap, stated in the system prompt: how long the answer should
    # roughly be. Overshooting it is fine — it is guidance, not a limit.
    output_guidance: str = ""


DEFINITIONS: dict[Role, RoleDefinition] = {
    Role.REQUIREMENTS: RoleDefinition(
        role=Role.REQUIREMENTS,
        label="Riley Requirements",
        color="#e0a458",
        prompt="requirements",
        # read-only workspace tools so an existing project is understood, not
        # re-specified from scratch; the only write is docs/discovery.md
        tools=(
            "ask_user",
            "web_search",
            "web_fetch",
            "list_dir",
            "glob",
            "grep",
            "read_file",
            "write_file",
        ),
        write_prefixes=("docs/",),
        permissions=role_rules(edit=("docs/*",), web=True),
        max_steps=5,
        output_guidance=(
            "Write a production-ready working spec: as many stories and "
            "acceptance criteria as the brief needs (each one line, with a "
            "named oracle). Prefer a coherent shippable product over a demo; "
            "capture the full intent of the brief."
        ),
    ),
    Role.PLANNER: RoleDefinition(
        role=Role.PLANNER,
        label="Pat Planner",
        color="#7aa2f7",
        prompt="planner",
        # Read-only exploration, one question if truly stuck, and writes
        # confined to the plan documents. The model's prompt narrows it to
        # exactly docs/plan.md; the permission ruleset must also cover
        # docs/contract.md because the orchestrator renders the contract under
        # this same scope (for transcript attribution) after the graph is
        # validated. The companion enforces the scope; the prompt advertises it.
        tools=("read_file", "list_dir", "glob", "grep", "ask_user", "write_file", "skill"),
        write_prefixes=("docs/plan.md", "docs/contract.md"),
        permissions=role_rules(edit=("docs/plan.md", "docs/contract.md")),
        max_steps=10,
        output_guidance=(
            "Plan the whole spec when it fits one run; defer acceptance "
            "criteria (the `deferred` list) only when it genuinely cannot, and "
            "never shrink the build to a demo. Two developers build in parallel: "
            "vertical feature slices, interfaces stubbed by the scaffold, "
            "depends_on for hard dependencies only. Prefer fewer sharp tasks. "
            "Non-overlapping owned_paths. Each description: 2–4 short lines "
            "(done-when, files, stubs/ids, must-not). plan.md = seams/schema/"
            "task table (+ deferred backlog if any), not a route dump. UI: "
            "scaffold owns docs/design.md + landing/CSS; one mount/bundle "
            "owner for third-party UI."
        ),
    ),
    Role.TEST_AUTHOR: RoleDefinition(
        role=Role.TEST_AUTHOR,
        label="Tessa Tests",
        color="#bb9af7",
        prompt="test_author",
        tools=("read_file", "write_file"),
        # tests/ is the suite; docs/browser-flows.json is the optional UI
        # verification plan the orchestrator writes under this same scope
        write_prefixes=("tests/", "docs/browser-flows.json"),
        permissions=role_rules(edit=("tests/*", "docs/browser-flows.json")),
        max_steps=15,
        output_guidance=(
            "Write a focused suite: roughly 3-6 test files covering the acceptance "
            "criteria, each file short. Cover the criteria; do not enumerate every case."
        ),
    ),
    Role.WORKER: RoleDefinition(
        role=Role.WORKER,
        label="Wren Worker",
        color="#9ece6a",
        prompt="worker",
        tools=DEV_TOOLS,
        # path ownership narrows this further per task, via the companion's leases
        permissions=role_rules(edit=("*",), deny_edit=("tests/*",), shell=True, web=True),
        max_steps=30,
        output_guidance=(
            "Implement only your task's owned paths — the files the task names and "
            "nothing more — and implement them completely. Keep each file focused."
        ),
    ),
    Role.FIXER: RoleDefinition(
        role=Role.FIXER,
        label="Felix Fixer",
        color="#f7768e",
        prompt="fixer",
        tools=FIX_TOOLS,
        permissions=role_rules(edit=("*",), deny_edit=("tests/*",), shell=True),
        max_steps=15,
        max_fix_attempts=3,
        output_guidance=(
            "Return a minimal patch: the smallest change to the smallest set of "
            "files that makes the failing tests pass."
        ),
    ),
    Role.REVIEWER: RoleDefinition(
        role=Role.REVIEWER,
        label="Remy Reviewer",
        color="#7dcfff",
        prompt="reviewer",
        # read-only: the reviewer inspects anything and edits nothing — its
        # verdict returns as JSON, evidence attached, never as a patch
        tools=("read_file", "list_dir", "glob", "grep", "submit_result"),
        permissions=role_rules(),
        max_steps=10,
        output_guidance=(
            "Report only evidence-backed gaps: the claim, what you observed "
            "instead, and the files involved. When the brief names integrated "
            "or clean UI, stub hosts and ignored design tokens are mandatory "
            "gaps — an empty list is valid only when those claims are met."
        ),
    ),
    Role.SUPERVISOR: RoleDefinition(
        role=Role.SUPERVISOR,
        label="Lena Lead",
        color="#e0af68",
        prompt="supervisor",
        # No tools of its own: one structured decision per escalation. The
        # orchestrator asks the user, amends the plan or runs an install on
        # its behalf, after validating the decision.
        tools=(),
        permissions=role_rules(),
        max_steps=1,
        output_guidance=(
            "One decision per escalation. Prefer answering from the spec, discovery "
            "and plan; ask the user only what those genuinely leave open, with 2-5 "
            "options and a recommended default. Amend the plan only for structure "
            "(split, add a helper task, fix a dependency, reassign) — never cut scope. "
            "Keep the message to the developer short and actionable."
        ),
    ),
    Role.REPORTER: RoleDefinition(
        role=Role.REPORTER,
        label="Rosa Reporter",
        color="#73daca",
        prompt="reporter",
        tools=("read_file", "glob", "grep", "submit_result"),
        write_prefixes=("README.md", "AGENTS.md", "docs/"),
        permissions=role_rules(edit=("README.md", "AGENTS.md", "docs/*")),
        max_steps=10,
        output_guidance=(
            "Keep the README, AGENTS.md, and cost report concise: a short "
            "overview, setup steps, and a brief cost summary — not an essay."
        ),
    ),
}


def definition_for(role: Role) -> RoleDefinition:
    try:
        return DEFINITIONS[role]
    except KeyError:
        raise ValueError(f"no definition for role {role}") from None


def system_prompt_for(
    role: Role,
    *,
    ui_product: bool = False,
    prod_ops: bool = False,
    security: bool = False,
    extra_skills: tuple[str, ...] = (),
    skill_mode: str = "embed",
    required_skills: tuple[str, ...] = (),
) -> str:
    """The role's system prompt with its output guidance appended.

    The guidance is the soft cap: it tells the model roughly how long the answer
    should be, in words the model actually follows. Overshooting it is fine —
    the hard ceiling lives in config and only stops a runaway. Skill packs
    append only when the matching flag is true and the role is in that skill's
    role set (callers use ``needs_*_skill`` heuristics).

    ``extra_skills`` carries the declarative set resolved from the Spec's
    frameworks (framework + quality packs, via ``skills.resolve_skills``). The
    three booleans stay for the heuristic product skills; the tuple adds the
    framework/quality packs without changing the boolean call sites.

    ``skill_mode="index"`` is for agents that have the ``skill`` tool: the
    same selection becomes an on-demand catalogue (recommended / required
    markers) instead of embedded packs. Tool-less roles keep ``"embed"``.
    """
    definition = definition_for(role)
    base = load_prompt(definition.prompt)
    parts = [base]
    if definition.output_guidance:
        parts.append(definition.output_guidance)
    if skill_mode == "index":
        recommended: list[str] = []
        if ui_product and role.value in UI_PRODUCT_ROLES:
            recommended.append("ui_product")
        if prod_ops and role.value in PROD_OPS_ROLES:
            recommended.append("prod_ops")
        if security and role.value in SECURITY_ROLES:
            recommended.append("security")
        recommended.extend(extra_skills)
        parts.append(skill_index_block(tuple(recommended), tuple(required_skills)))
        # Required packs are embedded, not loaded on demand: each load cost a
        # full turn (the whole brief re-sent) and was lost at every compaction,
        # so run-aa748f20's Dev A loaded the same two packs seven times. The
        # system prompt survives compaction.
        bodies = []
        for name in dict.fromkeys(required_skills):
            try:
                bodies.append(f"### skill: {name}\n{load_skill(name).strip()}")
            except (FileNotFoundError, ValueError):
                continue
        if bodies:
            parts.append(
                "Required skills for this task (already loaded — follow them; do not call "
                "the skill tool for these):\n\n" + "\n\n".join(bodies)
            )
        return "\n\n".join(parts)
    if ui_product and role.value in UI_PRODUCT_ROLES:
        parts.append(load_skill("ui_product"))
    if prod_ops and role.value in PROD_OPS_ROLES:
        parts.append(load_skill("prod_ops"))
    if security and role.value in SECURITY_ROLES:
        parts.append(load_skill("security"))
    for name in extra_skills:
        try:
            parts.append(load_skill(name))
        except (FileNotFoundError, ValueError):
            continue  # unknown/extra names are ignored; required ones are pre-validated
    return "\n\n".join(parts)


def may_write(role: Role, path: str, owned_paths: tuple[str, ...] = ()) -> bool:
    """Static write check: role defaults plus any per-task owned paths.

    Dynamic checks for a specific task assignment build on this; it is not a
    substitute for the companion-side leases and permission rules.
    """
    allowed = DEFINITIONS[role].write_prefixes + owned_paths
    return any(path == prefix or path.startswith(prefix) for prefix in allowed)
