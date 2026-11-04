"""Two developers ("lanes") sharing one plan.

The planner splits the spec into tasks; this module decides which of the two
developers takes each one, and keeps the team board both developers read:
who owns which task, its status, what each finished task delivered, and the
notes the developers send each other.

Assignment is deterministic:

1. A split product sends API-root tasks to Dev A (backend) and web-root tasks
   to Dev B (frontend) — the roots never overlap, so neither can block the other.
2. Everything else (single-root apps, API-only apps, fullstack roots, root-less
   tasks) is placed by list scheduling over the dependency graph: a task goes
   to the developer who can start it earliest; ties go to the developer who
   owns its dependencies (they already hold that context), then to Dev A. A
   pure chain stays with one developer — splitting it would only make the
   other wait — while independent branches run in parallel.

The board is the shared context (Claude Code's agent-team task list), and
``post`` is the message channel (its ``SendMessage``): a note reaches a busy
teammate before its next model call, or waits on the board for its next task.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from kiln.paths import paths_conflict
from kiln.schemas.tasks import Task, TaskGraph

LANE_KEYS = ("A", "B")
MAX_MEMORY = 6
MAX_BOARD_TASKS = 40
MAX_NOTES = 12


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def root_kind(spec, root: str) -> str:
    """'api', 'web' or 'shared' for the spec root a task builds in."""
    key = (root or "").strip().rstrip("/")
    if not key or key == "." or spec is None:
        return "shared"
    for declared in spec.roots or ():
        if (declared.path or "").strip().rstrip("/") == key:
            if declared.role == "api":
                return "api"
            if declared.role == "web":
                return "web"
            return "shared"
    return "shared"


def topological(graph: TaskGraph) -> list[Task]:
    """Tasks in dependency order, stable with respect to the planner's order."""
    placed: set[str] = set()
    ordered: list[Task] = []
    remaining = list(graph.tasks)
    known = {task.id for task in graph.tasks}
    while remaining:
        progressed = False
        for task in list(remaining):
            if all(dep in placed or dep not in known for dep in task.depends_on):
                ordered.append(task)
                placed.add(task.id)
                remaining.remove(task)
                progressed = True
        if not progressed:  # a cycle cannot pass TaskGraph validation; never loop forever
            ordered.extend(remaining)
            break
    return ordered


def assign_lanes(graph: TaskGraph, spec, lane_count: int = 2) -> tuple[dict[str, str], dict[str, str]]:
    """Map every task to a lane key and every lane key to its focus."""
    keys = LANE_KEYS[: max(1, min(lane_count, len(LANE_KEYS)))]
    order = topological(graph)
    if len(keys) == 1:
        return {task.id: keys[0] for task in order}, {keys[0]: "shared"}
    kinds = {task.id: root_kind(spec, task.root) for task in order}
    split = "api" in kinds.values() and "web" in kinds.values()
    fixed = (
        {tid: ("A" if kind == "api" else "B") for tid, kind in kinds.items() if kind != "shared"}
        if split
        else {}
    )
    assignment: dict[str, str] = {}
    free = {key: 0 for key in keys}
    finish: dict[str, int] = {}
    for task in order:
        deps = [dep for dep in task.depends_on if dep in finish]
        earliest = max((finish[dep] for dep in deps), default=0)
        if task.id in fixed:
            lane = fixed[task.id]
        else:
            starts = {key: max(free[key], earliest) for key in keys}
            best = min(starts.values())
            candidates = [key for key in keys if starts[key] == best]
            dep_lanes = {assignment[dep] for dep in deps}
            preferred = [key for key in candidates if key in dep_lanes]
            lane = (preferred or candidates)[0]
        begin = max(free[lane], earliest)
        finish[task.id] = begin + 1
        free[lane] = begin + 1
        assignment[task.id] = lane
    focus: dict[str, str] = {}
    for key in keys:
        lane_kinds = {kinds[tid] for tid, lane in assignment.items() if lane == key}
        if lane_kinds == {"api"}:
            focus[key] = "backend"
        elif lane_kinds == {"web"}:
            focus[key] = "frontend"
        else:
            focus[key] = "shared"
    return assignment, focus


def parallel_estimate(graph: TaskGraph, spec, lane_count: int = 2) -> tuple[int, int]:
    """(steps with ``lane_count`` developers, steps with one), unit-time tasks.

    A plan whose first number equals its second gives the second developer
    nothing to do in parallel — the signal to ask the planner for a
    parallel-friendly split.
    """
    assignment, _ = assign_lanes(graph, spec, lane_count)
    finish: dict[str, int] = {}
    free: dict[str, int] = {}
    for task in topological(graph):
        lane = assignment[task.id]
        earliest = max((finish[d] for d in task.depends_on if d in finish), default=0)
        start = max(free.get(lane, 0), earliest)
        finish[task.id] = start + 1
        free[lane] = start + 1
    return (max(finish.values(), default=0), len(graph.tasks))


@dataclass
class Lane:
    key: str
    agent_id: str
    focus: str
    current: str | None = None
    memory: list[dict] = field(default_factory=list)
    inbox: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"Dev {self.key}" + (f" ({self.focus})" if self.focus != "shared" else "")


@dataclass
class Question:
    qid: str
    asker: str
    to: str
    text: str
    answer: str | None = None
    event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class BoardTask:
    task: Task
    lane: str
    status: str = "pending"  # pending | running | done | failed
    summary: str = ""
    files: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)


class TeamBoard:
    """The shared view of the build: tasks, owners, statuses, notes, memory."""

    def __init__(self, graph: TaskGraph, spec, lane_count: int = 2):
        assignment, focus = assign_lanes(graph, spec, lane_count)
        self.lanes: dict[str, Lane] = {
            key: Lane(key=key, agent_id=f"worker-{index}", focus=focus[key])
            for index, key in enumerate(focus, start=1)
        }
        self.order = [task.id for task in topological(graph)]
        self.tasks: dict[str, BoardTask] = {
            task.id: BoardTask(task=task, lane=assignment[task.id]) for task in graph.tasks
        }
        self.notes: list[str] = []
        # files a lane borrowed for its current task (path -> reason), and the
        # files it is paused waiting for — the exclusive-checkout ledger
        self.borrowed: dict[str, dict[str, str]] = {key: {} for key in self.lanes}
        self.waiting: dict[str, set[str]] = {key: set() for key in self.lanes}
        # questions between the developers, and the one each is paused on
        self.questions: dict[str, Question] = {}
        self.awaiting: dict[str, str | None] = {key: None for key in self.lanes}
        # (category, from agent id, to agent id, text) for every message — the
        # floor renders them; never raises into the board
        self.listener: Callable[[str, str, str, str], None] | None = None
        self.contract_changes = 0
        self.decisions = 0

    # ── questions ───────────────────────────────────────────────────────────

    def ask(self, asker: str, to: str, text: str) -> str:
        qid = f"Q{len(self.questions) + 1}"
        self.questions[qid] = Question(qid=qid, asker=asker, to=to, text=_clip(text, 600))
        self.post(
            asker,
            to,
            f"question {qid}: {text} — answer with notify_peer(message, reply_to=\"{qid}\")",
            category="question",
        )
        return qid

    def answer(self, sender: str, qid: str, text: str) -> str:
        question = self.questions.get(qid.strip())
        if question is None:
            return f"no open question {qid}"
        if question.to != sender:
            return f"{qid} was asked of {self.lanes[question.to].label}, not you"
        if question.answer is not None:
            return f"{qid} is already answered"
        question.answer = _clip(text, 1200)
        question.event.set()
        if self.awaiting.get(question.asker) != qid:
            # the asker kept working: the answer arrives as a message
            self.post(sender, question.asker, f"answer to {qid}: {text}", category="answer")
        else:
            self._emit("answer", sender, question.asker, f"answer to {qid}: {text}")
        return f"answered {qid}"

    def drop_questions_to(self, key: str) -> None:
        """The answerer's task ended: wake anyone paused on its questions."""
        for question in self.questions.values():
            if question.to == key and question.answer is None:
                question.event.set()

    def is_waiting_on(self, a: str, b: str) -> bool:
        """Is developer ``a`` paused on developer ``b`` (an answer or a file)?"""
        qid = self.awaiting.get(a)
        if qid and self.questions[qid].to == b:
            return True
        return any(
            (holder := self.holder(path, a)) is not None and holder.key == b
            for path in self.waiting.get(a, ())
        )

    # ── shared files ────────────────────────────────────────────────────────

    def borrowed_paths(self, key: str) -> list[str]:
        return list(self.borrowed[key])

    def owner_task(self, path: str) -> BoardTask | None:
        """The planned task whose owned paths cover ``path``, if any."""
        for entry in self.tasks.values():
            if any(paths_conflict(path, owned) for owned in entry.task.owned_paths):
                return entry
        return None

    def holder(self, path: str, asking: str) -> Lane | None:
        """The teammate whose current lease covers ``path`` right now, if any."""
        for key, lane in self.lanes.items():
            if key == asking or lane.current is None:
                continue
            current = self.tasks[lane.current].task.owned_paths if lane.current in self.tasks else []
            held = [*current, *self.borrowed[key]]
            if any(paths_conflict(path, owned) for owned in held):
                return lane
        return None

    def blocked_by_borrow(self, task_id: str, key: str) -> bool:
        """A pending task may not start while a teammate has borrowed its files."""
        owned = self.tasks[task_id].task.owned_paths
        return any(
            paths_conflict(path, mine)
            for other, files in self.borrowed.items()
            if other != key
            for path in files
            for mine in owned
        )

    def clear_borrows(self, key: str) -> None:
        self.borrowed[key].clear()
        self.waiting[key].clear()

    # ── ownership and status ────────────────────────────────────────────────

    def lane_of(self, task_id: str) -> Lane:
        return self.lanes[self.tasks[task_id].lane]

    def other(self, key: str) -> Lane | None:
        return next((lane for k, lane in self.lanes.items() if k != key), None)

    def queue(self, key: str, subset: set[str] | None = None) -> list[str]:
        return [
            tid
            for tid in self.order
            if self.tasks[tid].lane == key
            and self.tasks[tid].status == "pending"
            and (subset is None or tid in subset)
        ]

    def ensure(self, task: Task) -> None:
        """Put a task on the board if it is not there (lighter lane; ties to A)."""
        if task.id in self.tasks:
            return
        load = {
            key: sum(1 for e in self.tasks.values() if e.lane == key and e.status == "pending")
            for key in self.lanes
        }
        lane = min(self.lanes, key=lambda key: (load[key], key))
        self.tasks[task.id] = BoardTask(task=task, lane=lane)
        self.order.append(task.id)

    def update_task(self, task: Task) -> None:
        """Replace a task's definition (e.g. a widened skeleton-fallback lease)."""
        if task.id in self.tasks:
            self.tasks[task.id].task = task

    def add_task(self, task: Task, lane: str | None = None) -> None:
        """A task the lead added mid-build: on the given lane, else the lighter one."""
        if task.id in self.tasks:
            self.tasks[task.id].task = task
            return
        self.ensure(task)
        if lane in self.lanes:
            self.tasks[task.id].lane = lane

    def remove_task(self, task_id: str) -> None:
        """A pending task the lead replaced with smaller ones."""
        self.tasks.pop(task_id, None)
        if task_id in self.order:
            self.order.remove(task_id)

    def start(self, task_id: str, key: str) -> None:
        entry = self.tasks[task_id]
        if entry.lane != key:
            previous = entry.lane
            entry.lane = key
            self.post(
                None, previous, f"{self.lanes[key].label} took over {task_id} from your queue",
                category="status",
            )
        entry.status = "running"
        self.lanes[key].current = task_id

    def finish(self, task_id: str, key: str, *, summary: str, files: list[str], gaps: list[str]) -> None:
        entry = self.tasks[task_id]
        entry.status = "done"
        entry.summary = _clip(summary, 300)
        entry.files = list(files)[:20]
        entry.gaps = [_clip(gap, 200) for gap in gaps][:6]
        lane = self.lanes[key]
        lane.current = None
        lane.memory.append(
            {
                "task_id": task_id,
                "title": entry.task.title,
                "summary": entry.summary,
                "files": entry.files[:12],
                "gaps": entry.gaps[:4],
            }
        )
        del lane.memory[:-MAX_MEMORY]
        teammate = self.other(key)
        if teammate is not None:
            listed = ", ".join(entry.files[:8]) or "no files"
            self.post(
                key, teammate.key, f"finished {task_id} ({entry.task.title}); files: {listed}",
                category="status",
            )

    def fail(self, task_id: str, key: str, reason: str) -> None:
        entry = self.tasks[task_id]
        entry.status = "failed"
        entry.summary = _clip(f"failed: {reason}", 300)
        self.lanes[key].current = None
        teammate = self.other(key)
        if teammate is not None:
            self.post(
                None, teammate.key, f"{task_id} ({entry.task.title}) failed and was not delivered",
                category="status",
            )

    def release(self, key: str) -> None:
        self.lanes[key].current = None

    def reassign(self, task_id: str, key: str) -> None:
        """Hand a pending/failed-attempt task to another lane (failover)."""
        entry = self.tasks[task_id]
        entry.lane = key
        entry.status = "pending"

    # ── messages ────────────────────────────────────────────────────────────

    def post(
        self, sender: str | None, recipient: str, text: str, *, category: str = "note",
        sender_label: str | None = None,
    ) -> str:
        """Deliver a note to a lane: now if it is mid-task, else at its next task.

        ``sender`` is a lane key; anything else (the lead, Kiln) passes None
        and an optional ``sender_label``.
        """
        lane = self.lanes.get(recipient)
        if lane is None:
            return "no teammate on this run"
        who = self.lanes[sender].label if sender in self.lanes else (sender_label or "Kiln")
        line = f"{who} → {lane.label}: {_clip(text, 600)}"
        self.notes.append(line)
        del self.notes[:-MAX_NOTES]
        from_id = self.lanes[sender].agent_id if sender in self.lanes else (
            "supervisor" if sender_label else "kiln"
        )
        self._emit(category, from_id, lane.agent_id, _clip(text, 600))
        if lane.current is not None:
            lane.inbox.append(f"{who}: {_clip(text, 600)}")
            return f"delivered to {lane.label} (working on {lane.current})"
        return f"{lane.label} is between tasks; your note is on the board for its next task"

    def _emit(self, category: str, from_id: str, to_id: str, text: str) -> None:
        if self.listener is None:
            return
        try:
            self.listener(category, from_id, to_id, text)
        except Exception:  # noqa: BLE001 — the floor must never break the board
            pass

    def snapshot(self) -> dict:
        """The Team panel's view: developers, tasks, counters."""
        lanes = []
        for key, lane in self.lanes.items():
            current = lane.current if lane.current in self.tasks else None
            lanes.append(
                {
                    "agent_id": lane.agent_id,
                    "label": lane.label,
                    "focus": lane.focus,
                    "current": current,
                    "current_title": self.tasks[current].task.title if current else "",
                    "checked_out": list(self.borrowed[key]),
                    "paused": bool(self.waiting[key]) or bool(self.awaiting.get(key)),
                }
            )
        tasks = [
            {
                "id": tid,
                "title": self.tasks[tid].task.title,
                "agent_id": self.lanes[self.tasks[tid].lane].agent_id,
                "status": self.tasks[tid].status,
            }
            for tid in self.order
        ]
        return {
            "lanes": lanes,
            "tasks": tasks,
            "open_questions": sum(1 for q in self.questions.values() if q.answer is None),
            "contract_changes": self.contract_changes,
            "decisions": self.decisions,
        }

    def drain(self, key: str) -> list[str]:
        lane = self.lanes[key]
        notes, lane.inbox = lane.inbox, []
        return notes

    # ── rendering ───────────────────────────────────────────────────────────

    def render_for(self, key: str, task_id: str | None = None) -> str:
        """The <team> block a developer reads at the start of a task."""
        me = self.lanes[key]
        teammate = self.other(key)
        lines = ["<team>"]
        if teammate is None:
            lines.append(f"You are {me.label}, the only developer on this run.")
        else:
            lines.append(
                f"You are {me.label} ({me.agent_id}). Your teammate is {teammate.label} "
                f"({teammate.agent_id}). You build in parallel in one workspace; each task's "
                "owned paths are leased to whoever runs it, so neither of you can write the "
                "other's files."
            )
        lines.append("Board (task · developer · status):")
        for tid in self.order[:MAX_BOARD_TASKS]:
            entry = self.tasks[tid]
            owner = self.lanes[entry.lane].label
            marker = " ← this task" if tid == task_id else ""
            line = f"- {tid} [{entry.task.kind}] {entry.task.title} · {owner} · {entry.status}{marker}"
            if entry.status == "pending" and entry.task.depends_on:
                line += f" (after {', '.join(entry.task.depends_on)})"
            if entry.status in ("running", "pending"):
                line += f" · owns {', '.join(entry.task.owned_paths[:6])}"
            if entry.status == "done":
                line += f" · {entry.summary}"
                if entry.files:
                    line += f" · files: {', '.join(entry.files[:8])}"
                if entry.gaps:
                    line += f" · gaps: {'; '.join(entry.gaps[:3])}"
            if entry.status == "failed":
                line += f" · {entry.summary}"
            lines.append(line)
        borrows = [
            f"- {self.lanes[k].label} is editing {path} ({why})"
            for k, files in self.borrowed.items()
            for path, why in files.items()
        ]
        if borrows:
            lines.append("Shared files checked out right now:")
            lines.extend(borrows)
        open_to_me = [q for q in self.questions.values() if q.to == key and q.answer is None]
        if open_to_me:
            lines.append("Questions waiting for your answer (notify_peer with reply_to):")
            lines.extend(
                f"- {q.qid} from {self.lanes[q.asker].label}: {q.text}" for q in open_to_me
            )
        answered = [q for q in self.questions.values() if q.asker == key and q.answer is not None]
        if answered:
            lines.append("Answers to your questions:")
            lines.extend(f"- {q.qid} {q.text} → {q.answer}" for q in answered[-6:])
        if len(self.order) > MAX_BOARD_TASKS:
            lines.append(f"- … (+{len(self.order) - MAX_BOARD_TASKS} more tasks)")
        if self.notes:
            lines.append("Recent notes:")
            lines.extend(f"- {note}" for note in self.notes[-MAX_NOTES:])
        if teammate is not None:
            lines.append(
                "Working together: stay inside your task's owned paths. To edit a file you "
                "do not own (a shared router, config or helper), use request_files: a free "
                "file is yours at once; a file your teammate is working on is queued — keep "
                "working on your other files and you will be told when it is yours (wait=true "
                "only when nothing else can move). When you change a seam your teammate uses "
                "(route, export, element id, env var), add a dependency, or establish a "
                "convention (error shape, naming, a shared helper), tell them with notify_peer. "
                "Before writing a helper, check the board and plan memo for one that exists."
            )
        lines.append("</team>")
        notes = self.lane_notes(key)
        return "\n".join(lines) + (f"\n\n{notes}" if notes else "")

    def render_overview(self) -> str:
        """The whole board as the tech lead sees it: both developers, every task."""
        lines = ["<team_board>"]
        for key, lane in self.lanes.items():
            state = f"working on {lane.current}" if lane.current else "between tasks"
            extra = f"; editing {', '.join(self.borrowed[key])}" if self.borrowed[key] else ""
            lines.append(f"{lane.label} ({lane.agent_id}): {state}{extra}")
        for tid in self.order[:MAX_BOARD_TASKS]:
            entry = self.tasks[tid]
            line = (
                f"- {tid} [{entry.task.kind}] {entry.task.title} · {self.lanes[entry.lane].label} · "
                f"{entry.status} · owns {', '.join(entry.task.owned_paths[:6])}"
            )
            if entry.task.depends_on:
                line += f" · after {', '.join(entry.task.depends_on)}"
            if entry.summary:
                line += f" · {entry.summary}"
            if entry.gaps:
                line += f" · gaps: {'; '.join(entry.gaps[:3])}"
            lines.append(line)
        open_questions = [q for q in self.questions.values() if q.answer is None]
        if open_questions:
            lines.append("Open questions between the developers:")
            lines.extend(f"- {q.qid} {self.lanes[q.asker].label}: {q.text}" for q in open_questions)
        if self.notes:
            lines.append("Recent notes:")
            lines.extend(f"- {note}" for note in self.notes[-MAX_NOTES:])
        lines.append("</team_board>")
        return "\n".join(lines)

    def lane_notes(self, key: str) -> str:
        memory = self.lanes[key].memory
        if not memory:
            return ""
        lines = ["<lane_notes>", "Your own earlier tasks this run:"]
        for item in memory:
            line = f"- {item['task_id']} {item['title']}: {item['summary']}"
            if item["files"]:
                line += f" · files: {', '.join(item['files'])}"
            if item["gaps"]:
                line += f" · gaps: {'; '.join(item['gaps'])}"
            lines.append(line)
        lines.append("</lane_notes>")
        return "\n".join(lines)

    def task_notes(self, task_ids: set[str]) -> str:
        """What the developers reported for these tasks, for the fixer."""
        lines = []
        for tid in self.order:
            if tid not in task_ids:
                continue
            entry = self.tasks[tid]
            if entry.status not in ("done", "failed"):
                continue
            owner = self.lanes[entry.lane].label
            line = f"- {tid} ({owner}): {entry.summary}"
            if entry.gaps:
                line += f" · gaps: {'; '.join(entry.gaps)}"
            lines.append(line)
        return "\n".join(lines)
