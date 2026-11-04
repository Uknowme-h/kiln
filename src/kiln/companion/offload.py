"""Write large tool outputs to workspace .kiln/tool-out/ and return a stub."""

from __future__ import annotations

import re
from pathlib import Path

OFFLOAD_DIR = ".kiln/tool-out"
HEAD_CHARS = 1_200
TAIL_CHARS = 800

# Tiny / status tools — never worth a file. read_file is here for a different
# reason: its output IS what the model asked for. Offloading it to another file
# of the same size only forces another read (run-afe452e5's worker spent five
# turns and ~60k prompt tokens reassembling one 12KB test file). read_file
# bounds itself instead: whole lines up to a budget, plus an offset hint.
SKIP_OFFLOAD_TOOLS = frozenset(
    {
        "claim_scope",
        "release_scope",
        "list_peers",
        "ask_user",
        "confirm_plan",
        "browse_host",
        "set_workspace",
        "probe_stack",
        "read_file",
    }
)

_SAFE = re.compile(r"[^a-zA-Z0-9._-]+")


def _safe_slug(value: str, *, fallback: str = "x") -> str:
    cleaned = _SAFE.sub("-", value.strip())[:48].strip("-")
    return cleaned or fallback


def format_offload_stub(path: str, text: str) -> str:
    n = len(text)
    head = text[:HEAD_CHARS]
    tail = text[-TAIL_CHARS:] if n > HEAD_CHARS + TAIL_CHARS else ""
    parts = [
        f"[tool output offloaded — {n} chars]",
        f"path: {path}",
        "--- head ---",
        head,
    ]
    if tail:
        parts += ["--- tail ---", tail]
    parts.append("Re-read with read_file if you need more.")
    return "\n".join(parts)


def maybe_offload_tool_output(
    root: Path,
    *,
    tool: str,
    output: str,
    agent_id: str = "agent",
    call_id: str = "",
    threshold: int = 8_000,
) -> str:
    """If ``output`` is large, persist it under ``.kiln/tool-out/`` and stub.

    On any filesystem error, returns the original ``output`` unchanged so the
    caller can fall back to in-loop clipping.
    """
    if tool in SKIP_OFFLOAD_TOOLS:
        return output
    if not isinstance(output, str) or len(output) < threshold:
        return output
    try:
        out_dir = root / OFFLOAD_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        name = (
            f"{_safe_slug(agent_id)}-{_safe_slug(tool)}-"
            f"{_safe_slug(call_id or 'out', fallback='out')}.txt"
        )
        path = out_dir / name
        path.write_text(output, encoding="utf-8", errors="replace")
        rel = f"{OFFLOAD_DIR}/{name}"
        return format_offload_stub(rel, output)
    except OSError:
        return output
