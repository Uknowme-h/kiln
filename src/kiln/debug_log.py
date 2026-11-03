"""Persisted per-run debug logs (gated by Settings.debug).

When ``DEBUG=true``, each pipeline run writes under ``.kiln/runs/{run_id}/``:

- ``meta.json`` — idea, workspace, timestamps
- ``transcript.jsonl`` — every model/tool entry (same payload as the drawer)
- ``usage.jsonl`` — every model-call token charge
- ``events.jsonl`` — floor SSE payloads
- ``summary.json`` — totals + by_role when the run finishes

Turn off with ``DEBUG=false`` (or omit); nothing is written.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

logger = logging.getLogger("kiln.debug")


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


class RunDebugLog:
    """Append-only JSONL writer for one run. Safe to call from worker threads."""

    def __init__(
        self,
        run_id: str,
        *,
        root: Path | None = None,
        idea: str = "",
        workspace: str = "",
    ) -> None:
        self.run_id = run_id
        self.root = Path(root) if root is not None else Path(".kiln") / "runs" / run_id
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self.started_at = datetime.now(UTC).isoformat()
        self._write_json(
            "meta.json",
            {
                "run_id": run_id,
                "idea": idea,
                "workspace": workspace,
                "started_at": self.started_at,
                "status": "building",
            },
        )
        logger.info("debug log → %s", self.root.resolve())

    def _append(self, name: str, row: dict) -> None:
        path = self.root / name
        line = json.dumps(_jsonable(row), ensure_ascii=False) + "\n"
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def _write_json(self, name: str, payload: dict) -> None:
        path = self.root / name
        with self._lock:
            path.write_text(
                json.dumps(_jsonable(payload), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

    def append_transcript(self, entry: Any) -> None:
        try:
            self._append(
                "transcript.jsonl",
                {
                    "seq": getattr(entry, "seq", None),
                    "agent_id": getattr(entry, "agent_id", ""),
                    "role": getattr(entry, "role", ""),
                    "kind": getattr(entry, "kind", ""),
                    "at": getattr(entry, "at", None),
                    "summary": entry.summary() if hasattr(entry, "summary") else "",
                    "payload": getattr(entry, "payload", {}),
                },
            )
        except Exception:
            logger.exception("failed to append transcript for %s", self.run_id)

    def append_usage(self, record: Any) -> None:
        try:
            if is_dataclass(record) and not isinstance(record, type):
                row = asdict(record)
            elif hasattr(record, "__dict__"):
                row = dict(record.__dict__)
            else:
                row = {"record": str(record)}
            self._append("usage.jsonl", row)
        except Exception:
            logger.exception("failed to append usage for %s", self.run_id)

    def append_event(self, event: Any) -> None:
        try:
            if hasattr(event, "model_dump"):
                row = event.model_dump(mode="json")
            elif isinstance(event, dict):
                row = event
            else:
                row = {"event": str(event)}
            row.setdefault("logged_at", datetime.now(UTC).isoformat())
            self._append("events.jsonl", row)
        except Exception:
            logger.exception("failed to append event for %s", self.run_id)

    def finish(
        self,
        *,
        status: str,
        green: bool | None = None,
        usage_summary: dict | None = None,
        wall_clock_seconds: float | None = None,
    ) -> Path:
        finished_at = datetime.now(UTC).isoformat()
        usage_rows = self._read_jsonl("usage.jsonl")
        by_role: dict[str, dict[str, int]] = {}
        by_agent: dict[str, dict[str, int]] = {}
        for row in usage_rows:
            role = str(row.get("role") or "unknown")
            agent = str(row.get("agent_id") or role)
            prompt = int(row.get("prompt_tokens") or 0)
            completion = int(row.get("completion_tokens") or 0)
            for bucket_key, store in ((role, by_role), (agent, by_agent)):
                bucket = store.setdefault(
                    bucket_key,
                    {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0},
                )
                bucket["calls"] += 1
                bucket["prompt_tokens"] += prompt
                bucket["completion_tokens"] += completion
        summary = {
            "run_id": self.run_id,
            "status": status,
            "green": green,
            "started_at": self.started_at,
            "finished_at": finished_at,
            "wall_clock_seconds": wall_clock_seconds,
            "usage": {
                "calls": len(usage_rows),
                "prompt_tokens": sum(int(r.get("prompt_tokens") or 0) for r in usage_rows),
                "completion_tokens": sum(
                    int(r.get("completion_tokens") or 0) for r in usage_rows
                ),
                "by_role": by_role,
                "by_agent": by_agent,
            },
            "process_usage_snapshot": usage_summary,
            "files": {
                "transcript": "transcript.jsonl",
                "usage": "usage.jsonl",
                "events": "events.jsonl",
            },
        }
        self._write_json("summary.json", summary)
        meta_path = self.root / "meta.json"
        meta = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
        meta.update(
            {
                "status": status,
                "green": green,
                "finished_at": finished_at,
            }
        )
        self._write_json("meta.json", meta)
        logger.info("debug summary → %s", (self.root / "summary.json").resolve())
        return self.root / "summary.json"

    def _read_jsonl(self, name: str) -> list[dict]:
        path = self.root / name
        if not path.is_file():
            return []
        rows: list[dict] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rows.append(json.loads(line))
        except Exception:
            logger.exception("failed to read %s", path)
        return rows


def open_run_debug_log(
    *,
    enabled: bool,
    run_id: str,
    idea: str = "",
    workspace: str = "",
    root: Path | None = None,
) -> RunDebugLog | None:
    if not enabled:
        return None
    try:
        return RunDebugLog(run_id, root=root, idea=idea, workspace=workspace)
    except Exception:
        logger.exception("could not open debug log for %s", run_id)
        return None
