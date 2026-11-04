"""The tech lead: an event-driven supervisor over the build.

The orchestrator is the deterministic supervisor (scheduling, retries,
failover, leases); the lead is the one who makes judgment calls. It wakes only
when someone escalates or the build hits trouble, makes one structured
decision, and the orchestrator executes it after validating it:

- answer / proceed  → a message to whoever escalated;
- ask_user          → one question to the user with options and a recommended
                      default (taken if they do not answer), recorded in
                      docs/discovery.md for every later task;
- amend_plan        → a structural plan change, rebuilt into a TaskGraph and
                      validated exactly like the original plan, then picked up
                      by the scheduler;
- install           → the stack's installer for a missing toolchain, with the
                      user's approval.

Only the lead may ask the user or change the plan mid-build. It never writes
code. Decisions are serialized and capped per run; past the cap (or on any
error) the escalation gets a deterministic "decide from the spec" reply, so
supervision can never stall or break a run.
"""

from __future__ import annotations

import asyncio
import json
import logging

from pydantic import ValidationError

from kiln.agents import supervisor
from kiln.agents.roles import Role
from kiln.events.schema import AgentRef, AgentStatus, ApprovalResolved, TeamEvent, TeamUpdate
from kiln.schemas.supervisor import Decision, Escalation, PlanAmendment, UserQuestion
from kiln.schemas.tasks import TaskGraph
from kiln.schemas.work import WorkFile

logger = logging.getLogger("kiln.lead")

LEAD = AgentRef(id="supervisor", role=Role.SUPERVISOR, label="Lena Lead")
LEAD_LABEL = "Lena Lead"
DECISIONS_HEADING = "## Decisions during the build"


def _parse_answer(raw: str, question: str) -> str:
    """The answer from the companion's 'User answered: "<q>"="<a>"' reply.

    Located by the exact question text, so quotes in either part are safe.
    """
    marker = f'"{question}"="'
    at = raw.find(marker)
    if at < 0:
        return ""
    rest = raw[at + len(marker):]
    return rest[:-1].strip() if rest.endswith('"') else rest.strip()


def _fallback(why: str) -> str:
    return (
        f"No tech lead decision ({why}). Decide from the spec, discovery and plan, "
        "record what you assumed in known_gaps, and continue."
    )


class LeadMixin:
    """Tech-lead behaviour for the Orchestrator (expects its attributes)."""

    # ── floor plumbing ──────────────────────────────────────────────────────

    def _spawn(self, coro) -> asyncio.Task | None:
        """A background coroutine that is kept alive and awaited before the run ends.

        Outside an event loop (a synchronous caller) there is nothing to run it
        on: it is closed instead of left un-awaited.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return None
        task = loop.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def _on_board_message(self, category: str, from_id: str, to_id: str, text: str) -> None:
        self._spawn(self.send(TeamEvent(category=category, from_agent=from_id, to_agent=to_id, text=text)))

    async def _team_event(self, category: str, from_id: str, to_id: str, text: str) -> None:
        await self.send(TeamEvent(category=category, from_agent=from_id, to_agent=to_id, text=text[:600]))

    async def _publish_team(self) -> None:
        if self._board is None:
            return
        try:
            await self.send(TeamUpdate(**self._board.snapshot()))
        except Exception:  # noqa: BLE001 — the floor never breaks the build
            logger.debug("team update failed", exc_info=True)

    async def _drain_background(self, timeout: float | None = None) -> None:
        pending = [task for task in self._background if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    # ── escalation ──────────────────────────────────────────────────────────

    def _start_supervision(self, escalation: Escalation) -> asyncio.Task:
        """Queue a decision. Registered at once (synchronously), so the
        scheduler never finishes a stage while a decision is on its way."""
        job = asyncio.get_running_loop().create_task(self._supervise(escalation))
        self._supervisor_jobs.add(job)
        job.add_done_callback(self._supervisor_jobs.discard)
        return job

    def _deliver_later(self, job: asyncio.Task, reply_lane: str | None) -> None:
        """Post the decision to a developer's inbox (or the board) when it lands."""

        def deliver(done: asyncio.Task) -> None:
            if reply_lane is None or self._board is None or done.cancelled():
                return
            try:
                message = done.result()
            except Exception as exc:  # noqa: BLE001
                message = _fallback(f"the lead failed: {exc}")
            self._board.post(None, reply_lane, message, category="decision", sender_label=LEAD_LABEL)

        job.add_done_callback(deliver)

    def _escalate_soon(self, escalation: Escalation, reply_lane: str | None) -> None:
        """A non-blocking escalation from synchronous bookkeeping code."""
        if not self.settings.enable_supervisor:
            return
        try:
            job = self._start_supervision(escalation)
        except RuntimeError:  # no running loop
            return
        self._deliver_later(job, reply_lane)

    async def _escalate(self, escalation: Escalation, *, wait: bool, reply_lane: str | None) -> str:
        """Hand a problem to the lead; wait for the decision or keep working."""
        job = self._start_supervision(escalation)
        if not wait:
            self._deliver_later(job, reply_lane)
            return "escalated to the tech lead; keep working — the decision arrives as a message"
        done, _ = await asyncio.wait({job}, timeout=self.settings.supervisor_wait_seconds)
        if job in done:
            try:
                return job.result()
            except Exception as exc:  # noqa: BLE001
                return _fallback(f"the lead failed: {exc}")
        self._deliver_later(job, reply_lane)
        return (
            "the tech lead is still deciding (it may be waiting on the user); continue with what "
            "you can — the decision arrives as a message"
        )

    async def _supervise(self, escalation: Escalation) -> str:
        async with self._supervisor_lock:
            if not self.settings.enable_supervisor:
                return _fallback("the tech lead is turned off")
            if self._supervisor_calls >= self.settings.supervisor_max_calls:
                return _fallback("the tech lead's budget for this run is used up")
            self._supervisor_calls += 1
            source = escalation.source or "kiln"
            await self.send(AgentStatus(agent=LEAD, status="thinking", detail=escalation.summary[:120]))
            await self._team_event("escalation", source, LEAD.id, f"[{escalation.kind}] {escalation.summary}")
            try:
                context = await self._lead_context(escalation)
                decision = await asyncio.to_thread(supervisor.decide, self.gateway, context)
            except Exception as exc:  # noqa: BLE001 — a lead failure never breaks the run
                logger.warning("tech lead failed on %r: %s", escalation.summary, exc)
                await self.send(AgentStatus(agent=LEAD, status="stuck", detail=str(exc)[:160]))
                return _fallback(f"the lead could not decide: {str(exc)[:200]}")
            message = await self._execute_decision(decision, escalation)
            line = f"[{escalation.kind}] {escalation.summary} → {decision.action}"
            self._decisions.append(f"{line}: {message[:240]}")
            self._run_notes.append(f"tech lead: {line}")
            if self._board is not None:
                self._board.decisions += 1
            await self._team_event("decision", LEAD.id, source, f"{decision.action}: {message[:400]}")
            await self.send(AgentStatus(agent=LEAD, status="done", detail=f"{decision.action}: {escalation.summary[:80]}"))
            await self.usage_tick()
            await self._publish_team()
            return f"Tech lead ({decision.action}): {message}"

    async def _lead_context(self, escalation: Escalation) -> str:
        from kiln.agents.requirements import DISCOVERY_DOC

        # read directly: the briefing cache is empty when injection is off
        docs = await self.read_workspace(self._run_id, [DISCOVERY_DOC]) if self._run_id else {}
        environment = ""
        if escalation.kind in ("environment", "blocked") and self._run_id:
            env = self._env(self._run_id)
            environment = f"OS: {env.get('os', '?')}; shell: {(env.get('shell') or {}).get('label', '?')}"
            probe = await self._probe(self.stack_id)
            if probe:
                environment += f"\nstack probe ({self.stack_id}): {json.dumps(probe)[:1500]}"
        return supervisor.build_context(
            idea=self._idea,
            spec=self._spec,
            discovery=docs.get(DISCOVERY_DOC, "") or "",
            plan_memo=self._plan_memo,
            board=self._board.render_overview() if self._board is not None else "",
            escalation=escalation,
            environment=environment,
            decisions=self._decisions,
        )

    async def _probe(self, stack_id: str) -> dict:
        try:
            raw = await self.call_tool(
                self._run_id, "probe_stack", {"stack_id": stack_id},
                agent=self._scope(LEAD.id, LEAD.label, Role.SUPERVISOR),
            )
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}

    # ── executing a decision ────────────────────────────────────────────────

    async def _execute_decision(self, decision: Decision, escalation: Escalation) -> str:
        message = decision.message
        if decision.action == "ask_user" and decision.question is not None:
            answer, answered = await self._ask_user_decision(decision.question)
            await self._record_decision(decision.question.question, answer, answered)
            how = "the user decided" if answered else "no answer from the user; taking the recommended option"
            return f"{message}\n{how}: {answer}"
        if decision.action == "amend_plan" and decision.amendment is not None:
            ok, info = await self._apply_amendment(decision.amendment)
            return f"{message}\n" + (f"Plan updated: {info}" if ok else f"The plan change was not applied: {info}")
        if decision.action == "install" and decision.install:
            return f"{message}\n{await self._install(decision.install)}"
        return message

    async def _ask_user_decision(self, question: UserQuestion) -> tuple[str, bool]:
        """(answer, answered by the user?) — the recommended option on silence."""
        if not self._run_id:
            return question.recommended, False
        minutes = max(1, round(self.settings.supervisor_user_timeout_seconds / 60))
        payload = {
            "questions": [
                {
                    "header": "Tech lead",
                    # the user may be away: say what happens if they are
                    "question": (
                        f"{question.question} (recommended: {question.recommended} — used if "
                        f"there is no answer within {minutes} min)"
                    ),
                    "options": list(question.options),
                    "multiple": False,
                }
            ],
            # the companion gives up on its own, so an absent user never holds
            # the prompt that every command approval queues behind
            "timeout_seconds": max(30, self.settings.supervisor_user_timeout_seconds),
        }
        try:
            raw = await asyncio.wait_for(
                self.call_tool(
                    self._run_id, "ask_user", payload,
                    agent=self._scope(LEAD.id, LEAD.label, Role.SUPERVISOR),
                ),
                # a safety net past the companion's own expiry (relay trouble)
                timeout=self.settings.supervisor_user_timeout_seconds + 60,
            )
        except Exception as exc:  # noqa: BLE001 — timeout, refusal or relay trouble
            logger.info("user question unanswered (%s); taking the recommendation", exc)
            if isinstance(exc, asyncio.TimeoutError):
                # the companion went silent past its own expiry, so it never
                # closed the floor's dialog: close it, the team has moved on
                await self.send(ApprovalResolved(agent_id=LEAD.id, approved=False))
            return question.recommended, False
        answer = _parse_answer(raw or "", payload["questions"][0]["question"])
        if not answer or answer == "(skipped)":
            return question.recommended, False
        return answer, True

    async def _record_decision(self, question: str, answer: str, answered: bool) -> None:
        """Append to docs/discovery.md so every later task sees the decision."""
        from kiln.agents.requirements import DISCOVERY_DOC

        current = (await self.read_workspace(self._run_id, [DISCOVERY_DOC])).get(DISCOVERY_DOC, "") or ""
        source = "user" if answered else "tech lead default (no answer)"
        entry = f"- {question} → {answer} ({source})"
        text = current.rstrip() + "\n\n" if current.strip() else ""
        if DECISIONS_HEADING not in text:
            text += f"{DECISIONS_HEADING}\n"
        text += entry + "\n"
        try:
            await self.write_files(self._run_id, [WorkFile(path=DISCOVERY_DOC, content=text)])
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not record the decision in %s: %s", DISCOVERY_DOC, exc)
        if self._board is not None:
            for lane in self._board.lanes:
                self._board.post(None, lane, f"product decision: {question} → {answer}",
                                 category="decision", sender_label=LEAD_LABEL)

    async def _install(self, tool: str) -> str:
        probe = await self._probe(self.stack_id)
        tools = probe.get("tools") or {}
        if tool not in tools:
            return f"cannot install {tool!r}: the {self.stack_id} stack does not use it"
        try:
            await self.call_tool(
                self._run_id, "install_tool", {"tool": tool},
                agent=self._scope(LEAD.id, LEAD.label, Role.SUPERVISOR),
            )
        except Exception as exc:  # noqa: BLE001
            return f"installing {tool} failed or was declined: {str(exc)[:200]}"
        self._run_notes.append(f"tech lead installed {tool}")
        return f"installed {tool}; retry your command (a new shell may be needed for PATH)"

    # ── plan amendments ─────────────────────────────────────────────────────

    async def _apply_amendment(self, amendment: PlanAmendment) -> tuple[bool, str]:
        """Validate and apply a structural plan change; (applied?, summary or reason)."""
        graph, board = self._graph, self._board
        if graph is None or board is None:
            return False, "there is no approved plan yet"
        if not self._building:
            return False, "the build stage is over; the fixer and reviewer handle the rest"

        def status(tid: str) -> str:
            return board.tasks[tid].status if tid in board.tasks else "pending"

        existing = {task.id for task in graph.tasks}
        replaced = amendment.replace_task
        # a pending task can be split; a failed one replaced by tasks that take over its files
        if replaced and (replaced not in existing or status(replaced) not in ("pending", "failed")):
            return False, f"{replaced} is running or done, so it cannot be replaced"
        new_ids = [task.id for task in amendment.add_tasks]
        clash = [tid for tid in new_ids if tid in existing and tid != replaced]
        if clash or len(new_ids) != len(set(new_ids)):
            return False, f"task ids already in use: {', '.join(clash) or 'duplicates'}"
        for change in amendment.dependencies:
            if change.task_id not in existing and change.task_id not in new_ids:
                return False, f"no task {change.task_id}"
            if change.task_id in existing and status(change.task_id) != "pending":
                return False, f"{change.task_id} has started; its dependencies are fixed"
        for move in amendment.reassign:
            if move.task_id in existing and status(move.task_id) != "pending":
                return False, f"{move.task_id} has started; it cannot change hands"
            if move.developer not in board.lanes:
                return False, f"there is no Dev {move.developer} on this run"

        replaced_deps = next((t.depends_on for t in graph.tasks if t.id == replaced), []) if replaced else []
        tasks = []
        for task in graph.tasks:
            if task.id == replaced:
                continue
            deps = list(task.depends_on)
            if replaced and replaced in deps:
                deps = [d for d in deps if d != replaced] + new_ids
            tasks.append(task.model_copy(update={"depends_on": deps}))
        for task in amendment.add_tasks:
            deps = [d for d in task.depends_on if d != replaced]
            if replaced and replaced in task.depends_on:
                deps += [d for d in replaced_deps if d not in deps]
            tasks.append(task.model_copy(update={"depends_on": deps}))
        by_id = {task.id: task for task in tasks}
        for change in amendment.dependencies:
            by_id[change.task_id] = by_id[change.task_id].model_copy(update={"depends_on": list(change.depends_on)})
        tasks = [by_id[task.id] for task in tasks]
        criterion_ids = [c.id for c in self._spec.acceptance_criteria] if self._spec else None
        try:
            rebuilt = TaskGraph.model_validate(
                {
                    "spec_title": graph.spec_title,
                    "tasks": [task.model_dump() for task in tasks],
                    "contract": graph.contract.model_dump() if graph.contract else None,
                    "deferred": list(graph.deferred),
                },
                context={"criterion_ids": criterion_ids} if criterion_ids else None,
            )
        except ValidationError as exc:
            reason = "; ".join(err.get("msg", "invalid") for err in exc.errors()[:3])
            return False, f"the amended plan is invalid: {reason}"

        graph.tasks[:] = rebuilt.tasks
        if replaced:
            board.remove_task(replaced)
            self._failed_tasks.pop(replaced, None)
        moves = {move.task_id: move.developer for move in amendment.reassign}
        for task in rebuilt.tasks:
            if task.id in board.tasks:
                board.update_task(task)
            else:
                board.add_task(task, moves.get(task.id))
                self._added_task_ids.add(task.id)
        for tid, lane in moves.items():
            if tid in board.tasks and board.tasks[tid].status == "pending":
                board.reassign(tid, lane)

        parts = []
        if replaced:
            parts.append(f"{replaced} split into {', '.join(new_ids)}")
        elif new_ids:
            parts.append(f"added {', '.join(new_ids)}")
        if amendment.dependencies:
            parts.append("dependencies changed for " + ", ".join(c.task_id for c in amendment.dependencies))
        if moves:
            parts.append("reassigned " + ", ".join(f"{t}→Dev {d}" for t, d in moves.items()))
        summary = "; ".join(parts) or "no change"
        self._run_notes.append(f"plan amended by the tech lead: {summary}")
        for lane in board.lanes:
            board.post(None, lane, f"PLAN CHANGED — {summary}", category="decision", sender_label=LEAD_LABEL)
        try:
            await self._publish_contract(self._run_id, graph)  # docs/contract.md lists the tasks
        except Exception:  # noqa: BLE001
            logger.debug("could not republish the contract doc", exc_info=True)
        await self._publish_team()
        return True, summary
