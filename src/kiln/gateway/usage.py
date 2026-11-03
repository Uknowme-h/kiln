"""Per-call token log. Prompt text is not stored."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock

from kiln.agents.roles import Role
from kiln.gateway.routing import Route


@dataclass(frozen=True)
class UsageRecord:
    role: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    fallback: bool
    at: str
    agent_id: str = ""


@dataclass
class UsageLog:
    records: list[UsageRecord] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock)
    # Optional sink (e.g. RunDebugLog.append_usage); never raises into the caller.
    listener: Callable[[UsageRecord], None] | None = None

    def record(
        self,
        *,
        role: Role,
        route: Route,
        prompt_tokens: int,
        completion_tokens: int,
        agent_id: str = "",
    ) -> UsageRecord:
        entry = UsageRecord(
            role=role.value,
            provider=route.provider,
            model=route.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            fallback=route.fallback,
            at=datetime.now(UTC).isoformat(),
            agent_id=agent_id or role.value,
        )
        with self._lock:
            self.records.append(entry)
        if self.listener is not None:
            try:
                self.listener(entry)
            except Exception:
                pass
        return entry

    def summary(self) -> dict:
        with self._lock:
            records = list(self.records)
        by_role: dict[str, dict[str, int]] = {}
        by_agent: dict[str, dict[str, int]] = {}
        for entry in records:
            for key, store in (
                (entry.role, by_role),
                (entry.agent_id or entry.role, by_agent),
            ):
                bucket = store.setdefault(
                    key, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
                )
                bucket["calls"] += 1
                bucket["prompt_tokens"] += entry.prompt_tokens
                bucket["completion_tokens"] += entry.completion_tokens
        return {
            "calls": len(records),
            "prompt_tokens": sum(entry.prompt_tokens for entry in records),
            "completion_tokens": sum(entry.completion_tokens for entry in records),
            "by_role": by_role,
            "by_agent": by_agent,
        }
