# Tech lead supervisor + floor integration — plan

## Problem

The orchestrator is a deterministic supervisor: it schedules, retries, fails
over, repairs and enforces leases, but cannot make a judgment call, and nobody
can reach the user mid-build. Concretely:

- **Shell / environment trouble** (tool not on PATH, port in use, permission
  denied, timeouts): the developer burns steps, then records a gap or fails.
- **Product questions**: developers have no `ask_user`; they guess.
- **Plan problems** (task too big, missing helper task, wrong dependency):
  the plan is frozen after approval; problems end as gaps or failed tasks.

And the floor (web UI) shows none of the collaboration added recently.

## What other systems do

| System | Supervision pattern | Taken |
|---|---|---|
| LangGraph "supervisor" | a central LLM routes work and decisions between worker agents | an LLM that decides, code that executes |
| AutoGen GroupChat manager / MetaGPT roles | a manager agent arbitrates; roles have fixed powers | the lead decides, never writes code |
| Claude Code agent teams | a lead coordinates teammates over a shared task list; only the lead talks to the user | only the lead may ask the user or change the plan mid-build |
| Devin / Claude Code `AskUserQuestion` | ask with options, continue on a sensible default | batched options + recommended default + timeout |
| CI / SRE runbooks | known failures get canned remediation before a human is paged | instant shell hints before any model call |

Always-on supervisors cost tokens on every step; Kiln's lead is
**event-driven**: it wakes only on an escalation or an automatic trigger.

## Design

### Role
`Role.SUPERVISOR` — "Lena Lead", on the planning (Spark) route. One structured
call per decision (no tool loop): it reads a compact context and returns a
`Decision`. It never writes code or runs commands; the orchestrator executes
and validates what it decides. Decisions are serialized (one at a time) and
capped per run (`supervisor_max_calls`, default 12); past the cap, developers
get a deterministic "decide from the spec, record the gap" reply.

### Triggers
1. `escalate(kind, summary, details, wait)` — a developer or the fixer.
   Kinds: `product`, `environment`, `plan`, `blocked`. `wait=true` (default)
   pauses the caller — no tokens — until the decision, up to
   `supervisor_wait_seconds`; past that it continues and the decision arrives
   as a message.
2. Automatic, non-blocking:
   - a task is finally not delivered (after retry and failover) → may add a
     replacement / split task so the build continues;
   - the same kind of shell failure recurs for a developer → environment
     escalation with the output.

### Instant shell hints (no model call)
The lane wrapper recognises common failures in shell output and appends a
hint: command not found / not recognized, port in use, permission denied,
timeout, missing module. The second failure of the same kind in one task
escalates automatically.

### Decision (structured output)
`action` ∈ `answer | ask_user | amend_plan | install | proceed`, a `message`
to the developer, plus:
- `question` {question, options (2–5), recommended} for `ask_user`: asked via
  the companion (`ask_user`, never auto-approved); on timeout or skip the
  recommended option is taken. The decision is appended to
  `docs/discovery.md` under "Decisions during the build" so every later task
  sees it.
- `amendment` for `amend_plan`: `add_tasks`, `replace_task` (a pending task
  split into the added tasks), `dependencies` (pending tasks only),
  `reassign` (pending tasks only). Validated by rebuilding the `TaskGraph`
  (ownership, cycles, scaffolds, coverage); dependents of a replaced task
  follow its replacements. Applied to the plan and the board; the scheduler
  picks new tasks up. Scope is never cut mid-build (the suite already judges
  every in-scope criterion): an unmeetable criterion becomes a known gap.
- `install` (a tool key from the stack probe) for `install`: run through the
  existing installer, with the user's approval.

### Context the lead reads
Idea, spec summary + criteria, discovery (clipped), plan memo (clipped), the
team board, the escalation, environment (OS, shell, stack probe for
environment issues), and its recent decisions.

## Floor (web UI) integration

New events (all small, SSE-safe):
- `team_event` {category, from_agent, to_agent, text} — notes between
  developers, questions/answers, file checkouts, contract changes,
  escalations, decisions. Rendered as envelopes between desks, the event log,
  the agent drawer's activity, and a team feed.
- `team_update` — snapshot: each developer (label, focus, current task,
  checked-out files, paused?) and every task's owner + status, open
  questions, contract changes, lead decisions. Rendered in a new **Team** panel.
- `run_finished.notes` — what went wrong and how the run recovered, shown on
  the Handoff screen.

Floor fixes: the lead gets a seat at the head of the meeting table and a
character; developer labels "Dev A" / "Dev B" stay distinct (they collapsed
to "Dev"); task cards use the developer's real label; the demo roster and
script show the two developers, a question, a contract change and a lead
decision.

## Edge cases

- Lead budget exhausted / model error / invalid decision → deterministic reply,
  build continues.
- Invalid amendment (overlap, cycle, dropped coverage, touches a running task)
  → refused with the reason; the developer gets the lead's message anyway.
- Amendment after the build stage ended → not applied ("the fixer and reviewer
  handle the rest").
- User does not answer → recommended default, recorded.
- Two escalations at once → queued.
- Escalation while the caller's task ends → decision delivered to the board.
- Install of a tool not in the stack's needs → refused.
- Single-developer runs: the lead works the same way.

## Found while implementing

- **A dangling question froze approvals.** The companion's prompt is one lock
  shared by questions and command approvals; a lead question the user never
  answered held it, so every later command waited forever. `ask_user` now
  takes `timeout_seconds`: the companion skips unanswered questions after it
  and releases the prompt; the server waits 60 s longer as a safety net, and
  only then closes the floor dialog itself (closing it twice could dismiss an
  unrelated approval). The question text names the recommendation and the
  deadline.
- **Escalation race.** Automatic escalations used to be spawned; the scheduler
  could see "no decision in flight" before the job existed and end the stage.
  The job is now registered synchronously (`_escalate_soon`).
- **Replaced tasks came back.** Stage B used the task list computed before an
  amendment, so `ensure()` re-added a replaced task. Stage B now takes the
  pending tasks of the amended plan.
- **Premature "dependencies never settled".** When nothing can start but a
  decision is pending, the scheduler waits for it before failing tasks.
- The lead reads `docs/discovery.md` directly (the briefing cache is empty
  when briefing injection is off); answers are parsed by the exact question
  text, so quotes are safe.

## Verification

Unit tests: shell hints and auto-escalation; each decision action; amendment
validation and application (split with dependents, reassign, refusal cases,
scheduler picks up new tasks); user question with default on timeout and the
discovery record; budget cap; serialization; event emission (`team_event`,
`team_update`, `run_finished.notes`); demo script validity. UI: label fix,
seat, panels (manual check in the browser). Full suite + `-m sim`.
