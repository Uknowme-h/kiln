"""Per-run transcript: every model call and tool call, in order, per agent.

The UI only ever consumes the floor event stream, which stays lightweight.
Transcripts are the heavy record behind it: when the user clicks an agent, the
drawer fetches this log over REST. Recording happens at the two choke points
that see everything — `Gateway.complete` for model I/O and the agent loop /
orchestrator for tool I/O — so every role is covered without per-agent plumbing.

In-memory and capped, matching the run registry's honesty: a transcript lives
exactly as long as its run and dies with the server.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Lock
from typing import Any, Literal

from pydantic import BaseModel, Field

# Per-field clip. A worker prompt is ~10k tokens; the full system+user block of
# one call must survive intact, so this is generous and only stops pathological
# payloads (a whole test report, a file dump) from ballooning the log.
FIELD_CLIP = 20_000
# Hard cap on stored entries; the run keeps going, oldest entries drop.
MAX_ENTRIES = 2_000


def _clip_text(text: str) -> str:
    if len(text) <= FIELD_CLIP:
        return text
    head = FIELD_CLIP * 2 // 3
    tail = FIELD_CLIP - head
    return f"{text[:head]}\n…[{len(text) - head - tail} chars clipped]…\n{text[-tail:]}"


def _clip_node(node: Any) -> Any:
    if isinstance(node, str):
        return _clip_text(node)
    if isinstance(node, dict):
        return {key: _clip_node(value) for key, value in node.items()}
    if isinstance(node, (list, tuple)):
        return [_clip_node(item) for item in node]
    return node


class TranscriptEntry(BaseModel):
    seq: int = Field(ge=1)
    agent_id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    kind: Literal["model_call", "tool_call", "tool_result", "note"]
    payload: dict = Field(default_factory=dict)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def summary(self) -> str:
        """One line for the floor tick and the drawer header."""
        if self.kind == "model_call":
            tokens = self.payload.get("total_tokens")
            calls = self.payload.get("tool_calls") or []
            if calls:
                names = ", ".join(call.get("name", "?") for call in calls)
                return f"model reply → {names}"
            suffix = f" ({tokens:,} tokens)" if isinstance(tokens, int) else ""
            if self.payload.get("truncated"):
                return f"model reply truncated ⚠{suffix}"
            return f"model reply{suffix}"
        if self.kind == "tool_call":
            return f"called {self.payload.get('name', '?')}"
        if self.kind == "tool_result":
            mark = "✗" if self.payload.get("error") else "✓"
            return f"{self.payload.get('name', '?')} {mark}"
        return str(self.payload.get("text", "note"))[:80]


class TranscriptLog:
    """Thread-safe ordered store. `Gateway.complete` runs in worker threads.

    An optional `listener` is invoked with each recorded entry (outside the
    lock). The orchestrator uses it to fire lightweight floor ticks; recording
    itself never depends on the listener succeeding.
    """

    def __init__(self, listener: Callable[[TranscriptEntry], None] | None = None) -> None:
        self._entries: list[TranscriptEntry] = []
        self._lock = Lock()
        self._listener = listener

    def record(
        self,
        agent_id: str,
        role: str,
        kind: str,
        payload: dict,
    ) -> TranscriptEntry:
        clipped = _clip_node(payload)
        with self._lock:
            entry = TranscriptEntry(
                seq=len(self._entries) + 1,
                agent_id=agent_id,
                role=role,
                kind=kind,  # type: ignore[arg-type]
                payload=clipped,
            )
            self._entries.append(entry)
            if len(self._entries) > MAX_ENTRIES:
                overflow = len(self._entries) - MAX_ENTRIES
                del self._entries[:overflow]
        if self._listener is not None:
            try:
                self._listener(entry)
            except Exception:
                pass  # a floor tick must never break the run that produced it
        return entry

    def agents(self) -> list[str]:
        with self._lock:
            seen: dict[str, None] = {}
            for entry in self._entries:
                seen.setdefault(entry.agent_id)
            return list(seen)

    def for_agent(self, agent_id: str, after: int = 0) -> list[TranscriptEntry]:
        with self._lock:
            return [
                entry
                for entry in self._entries
                if entry.agent_id == agent_id and entry.seq > after
            ]


def messages_payload(messages: list[dict]) -> list[dict]:
    """Deep-enough copy for storage; clipped on record."""
    return json.loads(json.dumps(messages, default=str))
