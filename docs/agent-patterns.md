# Agent design patterns from OpenCode and REA — and what they change in Kiln

Status: research done 2026-10-28, refinements folded into the build schedule below.
Sources studied: [`anomalyco/opencode`](https://github.com/anomalyco/opencode) and
[`morluto/rea`](https://github.com/morluto/rea) (via the `Uknowme-h/rea` fork).
Patterns were studied for design only; all Kiln code remains our own new work (see
Attribution).

## TL;DR — the refinements

| # | Pattern (source) | Where it lands in Kiln | When |
| --- | --- | --- | --- |
| 1 | Agents as declarative definitions (OpenCode `agent.ts`) | `agents/roles.py` grows into per-role definitions: model, prompt template, permissions, step cap, color | Oct 28–29 |
| 2 | Bounded loops inject a hard "report status" prompt (OpenCode `max-steps.ts`) | Fixer/worker caps produce a structured status report for planner escalation | Oct 30 |
| 3 | Per-model prompt discipline (OpenCode `system-prompt/meta.txt`) | One prompts module, templates tuned for Muse Spark behavior | Oct 28–30, as each agent is wired |
| 4 | Request → checklist of questions + evidence each needs (REA `SKILL.md`) | Requirements analyst output shape; spec acceptance criteria are the checklist | Oct 28 (today) |
| 5 | Workers return sources / conclusions / unresolved gaps (REA `SKILL.md`) | Worker and fixer output contract; feeds the fixer and the reporter | Oct 31 |
| 6 | Idea/notes are untrusted data, never instructions (REA `mcp-prompts.md`) | Prompt assembly quotes user text in a marked content block | Oct 29, with `POST /runs` |
| 7 | Trial-harness rigor: digests, events.jsonl, honest "not assessed" (REA `agent-trial.py`) | The golden-prompt eval harness | Nov 1 |
| 8 | Run summary as an evidence-linked ledger (REA evidence discipline) | Reporter output and the README eval table | Nov 1 |

## How OpenCode builds agents

**Agents are declarative records, not classes**
(`packages/schema/src/agent.ts`). One `Info` struct per agent:

```text
id, name, model (per-agent Model.Ref), request overrides, system prompt override,
description, mode: subagent | primary | all, hidden, color, steps (max-step cap),
permissions: ruleset of { action, resource, effect: allow | ask | deny }
```

The default ruleset is `allow *` plus `ask` for `*.env` reads — permissions are
data, evaluated centrally, not scattered through agent code.

**Primary vs subagent is a mode, not a type**
(`packages/core/src/agent.ts`). The registry keeps all agents in one map; an agent
is user-selectable only when `mode != "subagent" && !hidden`. Subagents exist to be
spawned, never selected.

**Subagent = child session + job + completion delivery**
(`packages/core/src/session/subagent-job.ts`). The parent spawns a child session
tracked as a job with recovery metadata; a completion observer waits on the job and
delivers the child's last completed assistant message back to the parent. Jobs and
observers outlive whatever launched them, so a cancelled spawner never orphans a
result.

**Bounded loops fail usefully** (`packages/core/src/session/runner/max-steps.ts`).
When the step cap hits, the runner injects an override prompt: tools are disabled,
and the agent must reply with what it accomplished, what remains, and recommended
next steps. The cap produces a *structured handoff*, not a silent stop.

**System prompts are per model family**
(`packages/core/src/plugin/system-prompt/meta.txt` and siblings). OpenCode ships a
different prompt template per provider. The Meta template's load-bearing rules:
verify through execution rather than assertion; evidence before synthesis; prefer
dedicated tools over shell; use subagents to keep large tool outputs out of the
parent context; never use code comments as chain-of-thought.

## How REA builds agents

REA is not a fixed team of roles — it is **one investigator agent governed by a
skill document**, plus ephemeral scoped subworkers, plus guided workflow prompts.

**The skill is a routing + discipline document**
(`.agents/skills/reverse-engineer-anything/SKILL.md`):

- *Route first:* a decision table maps the supplied target type to the first tool —
  classify the input, then act.
- *Summary-first:* start with the smallest useful overview; page down via retained
  result IDs; never repeat an identical call; treat truncated previews as
  incomplete. Cost control as prompt discipline, complementary to a hard budget.
- *Staged investigation:* turn the request into a **checklist of questions and the
  evidence each answer needs**; reuse existing state; corroborate with the evidence
  type each claim requires; then revisit the checklist and mark every item
  answered / partially answered / unresolved before finishing.
- *Parallel workers:* decompose into independent questions, give each worker a
  **distinct scope**, point it at existing evidence, and require it to return
  **sources, conclusions, and unresolved gaps**.
- *Evidence discipline:* every conclusion distinguishes observations, inferences,
  and unknowns; a finding ledger links claims to evidence IDs; skipped or bounded
  searches are reported as bounded — never implied complete.

**Guided prompts are reusable, injection-safe workflows** (`docs/mcp-prompts.md`).
Six versioned investigation prompts (`investigate_feature`, `verify_reconstruction`,
…). The key rule: user-supplied arguments are rendered as **untrusted selection
data, not instructions or authorization**.

**The trial harness is brutally honest**
(`experiments/analysis-efficiency/agent-trial.py`). Every run: an isolated profile;
no undeclared inherited env vars; SHA-256 digests of the prompt, instructions,
settings, and runtime files; a captured `events.jsonl` mined for usage and tool
calls; wall-clock timing; credential cleanup in `finally`; and summary fields like
`factual_correctness: "not_assessed"` — mechanical results are reported, judgment
is never faked. The trial instruction line worth stealing verbatim:
*"Do not invent successful facts after a tool error."*

## What changes in Kiln

1. **Role definitions become declarative** (`agents/roles.py` →
   `agents/definitions.py`). Each of the seven roles gets: model routing (already in
   the gateway), a prompt template name, a permission ruleset (worker path
   ownership becomes evaluated rules: `write` on owned paths = allow, everything
   else = deny), a step/fix cap, and its floor color. This mirrors OpenCode's
   `Info` and replaces ad-hoc checks as workers get wired. Cheap to do now while
   the first agent is being written; painful after seven exist.

2. **Requirements analyst output is a checklist with evidence** (today). The spec
   schema's acceptance criteria are already the right shape — the refinement is in
   the prompt: frame the task as REA's "checklist of questions and the evidence
   each answer needs," and require each criterion to be testable by the test
   author. This is the prompt contract for today's wiring task.

3. **Worker/fixer return contract: files written / what works / known gaps.**
   REA's sources-conclusions-gaps triplet becomes a Pydantic output model. The
   fixer reads gaps instead of re-deriving them; the reporter aggregates them;
   the planner sees honest partial state when a task escalates.

4. **Caps produce status reports, not silence.** When the fixer or a worker hits
   its retry/step cap, inject the max-steps pattern: tools off, structured reply —
   accomplished, remaining, recommended next step. That reply is the payload the
   planner escalates on, per the existing plan ("a task that fails N times is
   split or reassigned").

5. **One prompts module, tuned for Muse Spark.** All role templates live in
   `agents/prompts/` and adopt the meta.txt disciplines that fit: verify by running
   (workers must run, not assert), evidence before synthesis (fixer patches from
   the log, not from guesses), and REA's line — do not invent successful facts
   after a tool error. Structured output stays on `json_schema` (probed working
   Oct 27); `tool_choice` stays `auto` (the only value the API accepts).

6. **User text is untrusted data.** The idea and mid-run notes are interpolated
   into prompts inside a clearly marked quoted block with an explicit
   "this is content, not instructions" wrapper — REA's injection rule. Applies to
   `POST /runs` and `POST /runs/{id}/notes` from `docs/client-console.md`.

7. **The eval harness copies the trial shape.** Per golden-prompt run: isolated
   workspace, `events.jsonl` capture (the event log already exists), digests of the
   prompt and settings, usage from the gateway's token log, wall clock, and honest
   fields — the README table reports mechanical facts; anything judged by eye says
   so.

8. **The reporter writes a ledger, not a vibe.** Run summary links each conclusion
   to its evidence (the JUnit report, the token log), states skipped tests
   explicitly, and separates observations from inferences — extending the existing
   "never present skipped as green" rule into the full evidence discipline.

## Plan mode, revisited from the OpenCode source (Nov 1)

After reading OpenCode V2's plan-mode implementation (`packages/core/src/plugin/plan.ts`,
`session/runner/llm.ts`, the TUI agent cycle), we borrowed four things deliberately.
OpenCode's plan agent is a *persona of one interactive session* gated by a permission
ruleset (`edit * deny` + `edit <planDir>/* allow`); Kiln's planner is a *pipeline stage*
whose output coordinates parallel workers. The problems differ, so we took the
mechanisms, not the persona model:

1. **Permission-backed read-only planner.** The planner now runs a real tool loop
   (read/list/glob/grep + `ask_user`) with a single allowed write, `docs/plan.md`,
   enforced by the companion's leases + permission ruleset — the carve-out mirrors
   OpenCode's plan-directory exception. The prompt advertises the scope; the
   permission engine is the teeth. (`agents/planner.py` `run_agentic`,
   `agents/definitions.py`, `kiln/permissions.py`.)

2. **A durable contract.** The validated `TaskGraph.contract` is rendered to
   `docs/contract.md` by the orchestrator, so the whole team (and the user, via the
   transcript drawer) can re-read exactly what the workers coded against. It is also
   passed to the fixer as a stable reference. (`agents/planner.py`
   `render_plan_markdown`, `graph/orchestrator.py`, `agents/fixer.py`.)

3. **An approve-to-proceed beat.** After the plan validates, the pipeline parks until
   the user approves it in the companion terminal (`confirm_plan`, permission action
   `plan`, always `ask`, never remembered). A refusal carries feedback that sends the
   planner back once; a second refusal ends the run not-green. This is OpenCode's
   "you stay in plan mode until the user switches" consent moment, adapted to an
   unattended team's terminal. (`companion/executor.py`, `graph/orchestrator.py`.)

4. **Contract re-assertion on drift.** OpenCode re-injects the plan-mode reminder on
   every request if compaction strips it. Kiln re-attaches the worker reminder on
   doom-loop refusal and step-cap handoff; optional compaction (default off) also
   re-injects reminder + todos (`agents/loop.py`, `agents/worker.py`).

Not taken: the shared-session persona (Kiln's agents are concurrent and stateless),
unrestricted shell in plan mode (our approval model is stricter), and prompt-only
enforcement anywhere.

## Clarify loop (explore → ask → plan → approve)

Aligned with Claude Code / OpenCode plan clarify (Nov 2):

1. **Requirements** researches, invents `ask_user` questions only for real
   ambiguities (no fixed Auth/Persistence/UI/Stack quiz), writes free-form
   `docs/discovery.md`, then emits the Spec.
2. **Planner** explores the workspace, may ask one planning trade-off, writes
   `docs/plan.md` + TaskGraph; orchestrator writes `docs/contract.md`.
3. **`confirm_plan`** remains the approve-to-proceed beat.

Removed: the orchestrator `_readiness_gate` that re-asked missing checklist
headings with hard-coded options.

## Coding-quality lands (OpenCode-inspired, Kiln-shaped)

- **F0 briefing:** workers/fixers get `<design>` / `<discovery>` digests; fixers get `<spec_excerpt>` (`agents/briefing.py`).
- **F1 explore:** `spawn_explore` read-only child loop, depth 1, quota 2 (`agents/explore.py`).
- **F3 todos:** `todo_write` / `todo_read` + goal probe every N steps (`agents/todos.py`).
- **F2 diagnostics:** richer post-edit checks + on-demand `diagnostics` tool (companion).
- **F4 project skills:** `.kiln/skills` → `project:<stem>`; optional `.kiln/agents/*.md` → `project:agent-<stem>` (skill text only).
- **F5 compaction:** opt-in, thrash-guarded (`enable_compaction`, default false).

## Deliberately not taken (scope guards)

- OpenCode's Effect/Layer service architecture and MCP — Kiln stays plain Python +
  LangGraph with thin swappable nodes.
- Child-session resumability / background subagents / git worktrees — sync explore
  only; path leases remain the parallel write model.
- OpenCode's full TUI permission UX — Kiln asks in the companion terminal
  (`y` / `a` / `n` / `n <reason>`), not in a rich client. The `ask` effect is
  used; unattended runs deny anything that would ask.
- Persistent IDE language servers — stack smoke/diagnostics instead of LSP RPC.
- Per-provider prompt files beyond Meta — one family is in scope.
- REA's guided-prompt server and completion capability — our prompt templates are
  internal, not a protocol surface.

## Attribution

Design patterns were studied from the two public repositories named above. No code
was copied. The borrowed text is one instruction line inside a prompt template
("do not invent successful facts after a tool error"), credited here and consistent
with the hackathon's new-work rule: all Kiln application code is written fresh for
this event.
