"""In-memory event log for the live run view."""

from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock


@dataclass(frozen=True)
class RunEvent:
    id: int
    kind: str
    message: str
    at: str

    def public(self) -> dict[str, str | int]:
        return {"id": self.id, "kind": self.kind, "message": self.message, "at": self.at}


class EventLog:
    def __init__(self) -> None:
        self._events: list[RunEvent] = []
        self._lock = Lock()

    def append(self, kind: str, message: str) -> RunEvent:
        with self._lock:
            event = RunEvent(
                id=len(self._events) + 1,
                kind=kind,
                message=message,
                at=datetime.now(UTC).isoformat(),
            )
            self._events.append(event)
            return event

    def list(self) -> list[RunEvent]:
        with self._lock:
            return list(self._events)
