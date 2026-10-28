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

## Deliberately not taken (scope guards)

- OpenCode's Effect/Layer service architecture and MCP — Kiln stays plain Python +
  LangGraph with thin swappable nodes.
- Child-session resumability — Kiln's per-run workspace and event log are enough
  for a demo-scale system.
- OpenCode's `ask` permission effect — Kiln runs unattended; allow/deny only.
- Per-provider prompt files beyond Meta — one family is in scope.
- REA's guided-prompt server and completion capability — our prompt templates are
  internal, not a protocol surface.

## Attribution

Design patterns were studied from the two public repositories named above. No code
was copied. The borrowed text is one instruction line inside a prompt template
("do not invent successful facts after a tool error"), credited here and consistent
with the hackathon's new-work rule: all Kiln application code is written fresh for
this event.
