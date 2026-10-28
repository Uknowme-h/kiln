"""Declarative definition of every role on the floor.

Model routing stays in `gateway/routing.py`; prompts live in `agents/prompts/`.
Everything else about a role is data here: which tools it may call, which
workspace prefixes it may write by default, and its caps. Workers and the fixer
also receive per-task owned paths at assignment time; those are evaluated on
top of these defaults, not instead of them.
"""

from dataclasses import dataclass

from kiln.agents.roles import Role


@dataclass(frozen=True)
class RoleDefinition:
    role: Role
    label: str
    color: str
    prompt: str
    tools: tuple[str, ...] = ()
    write_prefixes: tuple[str, ...] = ()
    max_steps: int = 20
    max_fix_attempts: int = 0
    # per-role output budget; None uses the gateway default
    max_output_tokens: int | None = None


DEFINITIONS: dict[Role, RoleDefinition] = {
    Role.REQUIREMENTS: RoleDefinition(
        role=Role.REQUIREMENTS,
        label="Riley Requirements",
        color="#e0a458",
        prompt="requirements",
        write_prefixes=("docs/",),
        max_steps=5,
    ),
    Role.PLANNER: RoleDefinition(
        role=Role.PLANNER,
        label="Pat Planner",
        color="#7aa2f7",
        prompt="planner",
        write_prefixes=("docs/",),
        max_steps=10,
    ),
    Role.TEST_AUTHOR: RoleDefinition(
        role=Role.TEST_AUTHOR,
        label="Tessa Tests",
        color="#bb9af7",
        prompt="test_author",
        tools=("read_file", "write_file"),
        write_prefixes=("tests/",),
        max_steps=15,
        max_output_tokens=12_288,  # a full suite is the largest single output
    ),
    Role.WORKER: RoleDefinition(
        role=Role.WORKER,
        label="Wren Worker",
        color="#9ece6a",
        prompt="worker",
        tools=("read_file", "write_file", "shell_command"),
        max_steps=30,
    ),
    Role.FIXER: RoleDefinition(
        role=Role.FIXER,
        label="Felix Fixer",
        color="#f7768e",
        prompt="fixer",
        tools=("read_file", "write_file"),
        max_steps=15,
        max_fix_attempts=3,
    ),
    Role.REPORTER: RoleDefinition(
        role=Role.REPORTER,
        label="Rosa Reporter",
        color="#73daca",
        prompt="reporter",
        tools=("read_file",),
        write_prefixes=("README.md", "docs/"),
        max_steps=10,
    ),
}


def definition_for(role: Role) -> RoleDefinition:
    try:
        return DEFINITIONS[role]
    except KeyError:
        raise ValueError(f"no definition for role {role}") from None


def may_write(role: Role, path: str, owned_paths: tuple[str, ...] = ()) -> bool:
    """Static write check: role defaults plus any per-task owned paths.

    Dynamic checks for a specific task assignment build on this; it is not a
    substitute for the sandbox or for companion-side validation.
    """
    allowed = DEFINITIONS[role].write_prefixes + owned_paths
    return any(path == prefix or path.startswith(prefix) for prefix in allowed)
