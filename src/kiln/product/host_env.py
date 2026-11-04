"""Host-environment product gaps that code agents cannot fix.

Missing Playwright, browsers, or a spawnable start binary are machine setup
problems. Treating them as brief-review gaps forces the fixer into a no-op
and can abort a green run. Soft-skip / warn instead.
"""

from __future__ import annotations

__all__ = [
    "BLOCKED_HOST_SETUP",
    "is_blocked_host_setup",
    "is_host_env_gap",
    "partition_product_gaps",
]

# A gate that could not run because the host is not provisioned (no Playwright
# / Chromium, no start command). Distinct from host_env *findings*: this is the
# gate's own status, and for a UI product it must block a green verdict rather
# than be waved through as advisory.
BLOCKED_HOST_SETUP = "blocked_host_setup"

_HOST_ENV_MARKERS = (
    "playwright is not installed",
    "playwright browsers missing",
    "executable not found on path",
    "could not spawn start command",
    "filenotfounderror",
    "the system cannot find the file specified",
    "winerror 2",
)

_BLOCKED_MARKERS = (BLOCKED_HOST_SETUP,)


def is_blocked_host_setup(gap: str) -> bool:
    text = gap.lower()
    return any(marker in text for marker in _BLOCKED_MARKERS)


def is_host_env_gap(gap: str) -> bool:
    text = gap.lower()
    return any(marker in text for marker in _HOST_ENV_MARKERS)


def partition_product_gaps(gaps: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Split gaps into (actionable, host_env, blocked_host_setup).

    actionable  — a code defect the fixer can patch.
    host_env    — a machine-setup finding; advisory, never forces a fix round.
    blocked     — a required gate that could not run; for UI products this must
                  stop a green verdict until the user accepts the limitation.
    """
    actionable: list[str] = []
    host_env: list[str] = []
    blocked: list[str] = []
    for gap in gaps:
        if is_blocked_host_setup(gap):
            blocked.append(gap)
        elif is_host_env_gap(gap):
            host_env.append(gap)
        else:
            actionable.append(gap)
    return actionable, host_env, blocked
