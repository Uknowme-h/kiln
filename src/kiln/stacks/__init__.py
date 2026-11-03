"""Stack profiles: how to detect, install, test, and smoke-check each supported stack."""

from kiln.stacks.profiles import (
    PROFILES,
    STACK_IDS,
    StackProfile,
    ToolNeed,
    install_command,
    parse_version,
    profile_for,
)

__all__ = [
    "PROFILES",
    "STACK_IDS",
    "StackProfile",
    "ToolNeed",
    "install_command",
    "parse_version",
    "profile_for",
]
