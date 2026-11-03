"""Host-environment product gaps that code agents cannot fix.

Missing Playwright, browsers, or a spawnable start binary are machine setup
problems. Treating them as brief-review gaps forces the fixer into a no-op
and can abort a green run. Soft-skip / warn instead.
"""

from __future__ import annotations

__all__ = ["is_host_env_gap", "partition_product_gaps"]

_HOST_ENV_MARKERS = (
    "playwright is not installed",
    "playwright browsers missing",
    "executable not found on path",
    "could not spawn start command",
    "filenotfounderror",
    "the system cannot find the file specified",
    "winerror 2",
)


def is_host_env_gap(gap: str) -> bool:
    text = gap.lower()
    return any(marker in text for marker in _HOST_ENV_MARKERS)


def partition_product_gaps(gaps: list[str]) -> tuple[list[str], list[str]]:
    """Split gaps into (actionable, host_env)."""
    actionable: list[str] = []
    host_env: list[str] = []
    for gap in gaps:
        (host_env if is_host_env_gap(gap) else actionable).append(gap)
    return actionable, host_env
