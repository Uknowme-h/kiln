"""Who may write where, enforced when the write happens.

A dev agent is leased the paths its task owns. Every write or edit is checked
against the lease table at the moment it runs, so an agent cannot overwrite a
peer's file even if its prompt is talked into trying, and the refusal explains
who owns the path so the agent can record a gap instead of retrying.

Also here: a per-path mutex (concurrent writers to one file take turns) and a
read-before-write ledger (an edit is refused if the file changed since the agent
last read it, so a peer's concurrent change is never silently lost).
"""

import hashlib
import threading
from contextlib import contextmanager

from kiln.paths import paths_conflict, safe_relative_path


class LeaseError(RuntimeError):
    """A write outside the caller's lease, or a claim that collides with a peer's."""


def covers(prefix: str, path: str) -> bool:
    """True when `path` is the leased file itself or lives under the leased folder.

    A prefix of `.` means the whole workspace.
    """
    if prefix in ("", "."):
        return True
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def _normalize_lease(path: str) -> str:
    """Owned paths are workspace-relative; `.` means the whole workspace."""
    raw = path.strip().replace("\\", "/").rstrip("/")
    if raw in ("", "."):
        return "."
    return safe_relative_path(raw)


class LeaseTable:
    def __init__(self) -> None:
        self._prefixes: dict[str, tuple[str, ...]] = {}
        self._labels: dict[str, str] = {}
        self._lock = threading.Lock()

    def claim(self, agent_id: str, label: str, owned: list[str]) -> None:
        """Lease paths to an agent. Re-claiming the same paths is a no-op; colliding with
        another agent's lease is an error."""
        prefixes = tuple(_normalize_lease(path) for path in owned)
        with self._lock:
            for other, theirs in self._prefixes.items():
                if other == agent_id:
                    continue
                for mine in prefixes:
                    for taken in theirs:
                        if paths_conflict(mine, taken):
                            raise LeaseError(
                                f"{mine} overlaps {taken}, already leased to "
                                f"{self._labels.get(other, other)}"
                            )
            self._prefixes[agent_id] = prefixes
            self._labels[agent_id] = label or agent_id

    def release(self, agent_id: str) -> None:
        with self._lock:
            self._prefixes.pop(agent_id, None)
            self._labels.pop(agent_id, None)

    def owner_of(self, path: str) -> tuple[str, str] | None:
        with self._lock:
            for agent, prefixes in self._prefixes.items():
                if any(covers(prefix, path) for prefix in prefixes):
                    return agent, self._labels.get(agent, agent)
        return None

    def check_write(self, agent_id: str, path: str) -> None:
        with self._lock:
            mine = self._prefixes.get(agent_id)
        if mine is None:
            raise LeaseError(f"{agent_id} has no lease; no write is allowed")
        if any(covers(prefix, path) for prefix in mine):
            return
        owner = self.owner_of(path)
        if owner is not None:
            raise LeaseError(
                f"{path} is owned by {owner[1]} ({owner[0]}). You may not write it; record what "
                "you need from it in known_gaps, or look at it with read_file."
            )
        raise LeaseError(
            f"{path} is outside your owned paths ({', '.join(mine)}). "
            "Write only inside your owned paths."
        )

    def prefixes_of(self, agent_id: str) -> tuple[str, ...]:
        with self._lock:
            return self._prefixes.get(agent_id, ())

    def all_prefixes(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(prefix for prefixes in self._prefixes.values() for prefix in prefixes)

    def active_agents(self) -> list[str]:
        with self._lock:
            return list(self._prefixes)


class KeyedLocks:
    """One lock per path, created on demand."""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    @contextmanager
    def hold(self, key: str):
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            yield


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ReadLedger:
    """What each agent last saw of each file, as a content hash."""

    def __init__(self) -> None:
        self._seen: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def record(self, agent_id: str, path: str, text: str) -> None:
        with self._lock:
            self._seen[(agent_id, path)] = digest(text)

    def check(self, agent_id: str, path: str, current_text: str) -> str | None:
        """None if the agent may modify the file; otherwise why not."""
        with self._lock:
            seen = self._seen.get((agent_id, path))
        if seen is None:
            return f"read {path} with read_file before you change it"
        if seen != digest(current_text):
            return f"{path} changed since you read it; read it again before editing"
        return None
