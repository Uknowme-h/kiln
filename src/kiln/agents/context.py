"""Context hygiene for agent loops: prune stale tool bodies (OpenCode-style).

Compaction (summarize) lives in ``loop.py``. This module only rewrites old
``role=tool`` contents in place so each ``gateway.complete`` pays less for
history the model will not need verbatim.
"""

from __future__ import annotations

PRUNED_PREFIX = "[tool output pruned"
OFFLOADED_PREFIX = "[tool output offloaded"
DEFAULT_PRUNE_PROTECT_CHARS = 40_000
DEFAULT_PROTECT_TURNS = 2
# Leave tiny tool acks alone (e.g. "accepted", lease notes).
MIN_PRUNE_CHARS = 400


def _is_already_slim(content: str) -> bool:
    return content.startswith(PRUNED_PREFIX) or content.startswith(OFFLOADED_PREFIX)


def prune_tool_results(
    messages: list[dict],
    *,
    protect_chars: int = DEFAULT_PRUNE_PROTECT_CHARS,
    protect_turns: int = DEFAULT_PROTECT_TURNS,
    min_reclaim_chars: int = 0,
) -> int:
    """Rewrite older tool-role contents to a short placeholder.

    Protects:
    - the last ``protect_turns`` assistant messages and every tool message
      that follows them until the next assistant (a turn)
    - any remaining recent tool messages that still fit inside
      ``protect_chars`` counted from the end of the transcript

    ``min_reclaim_chars`` batches the rewrite: nothing is pruned until the
    prunable bodies add up to at least that much. Every rewrite changes the
    history prefix a provider may have cached, so pruning a little each step
    costs more than it saves.

    Returns the number of tool messages pruned. Idempotent.
    """
    if not messages or protect_chars < 0:
        return 0

    protected: set[int] = set()
    # Mark last N assistant turns (assistant + following tools) as protected.
    turns = 0
    i = len(messages) - 1
    while i >= 0 and turns < protect_turns:
        msg = messages[i]
        if msg.get("role") == "assistant":
            protected.add(i)
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                protected.add(j)
                j += 1
            turns += 1
            i -= 1
            continue
        if msg.get("role") == "tool":
            # Walk back to the owning assistant later; for now keep walking.
            i -= 1
            continue
        i -= 1

    # Char budget from the end: keep recent tool bodies even outside turn window.
    budget = protect_chars
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        size = len(content)
        if msg.get("role") == "tool" and idx not in protected:
            if budget >= size:
                protected.add(idx)
                budget -= size
            # else: leave unprotected → candidate for prune
        elif msg.get("role") in ("assistant", "user", "system"):
            # Still consume budget so "recent" tracks wall of transcript.
            if budget > 0:
                budget = max(0, budget - min(size, budget))

    candidates = []
    for idx, msg in enumerate(messages):
        if msg.get("role") != "tool" or idx in protected:
            continue
        content = msg.get("content")
        if not isinstance(content, str) or len(content) < MIN_PRUNE_CHARS:
            continue
        if _is_already_slim(content):
            continue
        candidates.append(idx)
    if not candidates:
        return 0
    if sum(len(messages[idx]["content"]) for idx in candidates) < min_reclaim_chars:
        return 0

    pruned = 0
    for idx in candidates:
        msg = messages[idx]
        content = msg["content"]
        call_id = msg.get("tool_call_id") or ""
        tag = f" id={call_id}" if call_id else ""
        messages[idx] = {
            **msg,
            "content": (
                f"{PRUNED_PREFIX}{tag} — {len(content)} chars removed. "
                "Re-read the path or re-run the tool if you still need the body.]"
            ),
        }
        pruned += 1
    return pruned
