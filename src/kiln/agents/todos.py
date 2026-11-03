"""In-loop todo checklist for coding agents (F3)."""

from __future__ import annotations

import json
from threading import Lock

from kiln.tools.schemas import TodoItem

_VALID = frozenset({"pending", "in_progress", "done"})
_MAX_ITEMS = 12


class TodoStore:
    """Thread-safe checklist keyed by agent_id within one parent task."""

    def __init__(self) -> None:
        self._items: list[TodoItem] = []
        self._lock = Lock()

    def read(self) -> list[TodoItem]:
        with self._lock:
            return [item.model_copy() for item in self._items]

    def write(self, items: list[TodoItem], *, merge: bool) -> str | None:
        cleaned: list[TodoItem] = []
        for item in items:
            status = (item.status or "pending").strip().lower()
            if status not in _VALID:
                return f"invalid status {item.status!r}; use pending|in_progress|done"
            cleaned.append(
                TodoItem(id=item.id.strip(), content=item.content.strip(), status=status)
            )
        if not cleaned:
            return "todo_write needs at least one item"
        with self._lock:
            if merge:
                by_id = {item.id: item for item in self._items}
                for item in cleaned:
                    by_id[item.id] = item
                merged = list(by_id.values())
            else:
                merged = cleaned
            if len(merged) > _MAX_ITEMS:
                return f"at most {_MAX_ITEMS} todo items"
            if sum(1 for item in merged if item.status == "in_progress") > 1:
                return "at most one todo may be in_progress"
            self._items = merged
        return None

    def open_items(self) -> list[TodoItem]:
        return [item for item in self.read() if item.status != "done"]

    def render(self) -> str:
        items = self.read()
        if not items:
            return "(no todos)"
        lines = []
        for item in items:
            mark = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}.get(
                item.status, "[?]"
            )
            lines.append(f"{mark} {item.id}: {item.content}")
        return "\n".join(lines)

    def open_as_gaps(self) -> list[str]:
        return [f"todo {item.id}: {item.content}" for item in self.open_items()]

    def to_json(self) -> str:
        return json.dumps([item.model_dump() for item in self.read()], indent=2)
