# Architecture research: published methods applied to Kiln

Status: researched and landed 2026-11-02 (items 1–8 plus both micro-fixes,
then items 9–13 the same day) against the working tree (`pytest`: 237
passing; `ruff check src tests` unchanged at its 23 pre-existing errors).
Item 7 is **partial** (counts-first landed; JUnit-path/fixer-loop deferred).
Item 12’s Playwright *inside the hermetic Docker suite* remains **declined**;
a **host-only structured FlowPlan** gate (`browser_verify`) later landed as
the click-through alternative. The product-quality
items were folded in after a yes-app postmortem (generic note app: green
vitest, weak UX/auth/Excalidraw). See **Implementation status** after the
table for what each item changed and where the build differs from the plan;
`file.py:line` anchors in the Source sections are plan-time citations kept
for history.
Companion to `docs/agent-patterns.md` (OpenCode and REA patterns); this file
studies three Anthropic engineering posts about multi-agent architecture and
maps each method onto Kiln's own pipeline, then adds product-quality methods
drawn from OpenCode's plan/explore patterns, Cursor/Claude verification
practice, and dual-track discovery → delivery as product-management practice.

Sources studied:

1. *Building effective agents* (Anthropic, Dec 2024) — workflow patterns.
2. *How we built our multi-agent research system* (Anthropic, Jun 2025) —
   orchestrator-workers discipline, delegation, token economics.
3. *Building a C compiler with a team of parallel Claudes* (Anthropic, Feb 2026)
   — parallel coders sharing one codebase without a human in the loop.
4. OpenCode's plan/explore agent patterns (same repo study as
   `agent-patterns.md`); verify-with-executable-checks practice from
   Cursor / Claude Code workflows; dual-track discovery → delivery as
   product-management practice.

## TL;DR — build order

The table is the original plan. **Implementation status** below is the
source of truth for what shipped.

| # | Method (source) | Where it lands in Kiln | Effort |
| --- | --- | --- | --- |
| 1 | CI after every increment; never break old work (C compiler) | Suite after each layer but the last; `install_deps` after scaffold; hard `smoke_check` failures; `<test_state>` to the next layer | medium |
| 2 | A running doc of failed approaches (C compiler) | `fixer.run(history=…, known_gaps=…)` → `<previous_attempts>` / `<known_gaps>` | small |
| 3 | Artifacts to the filesystem, references through the coordinator (Research system) | Agentic workers get dependency **paths** not contents; reporter was already paths-only; `run_agentic` re-read kept (Python-side write check) | small–medium |
| 4 | Ground truth before claiming done (Effective agents) | `worker.txt`: run the suite before `submit_result`; never submit on assertion | trivial |
| 5 | Scale effort to task complexity (Research system) | Sizing reconciled in **both** `planner.txt` rule 4 and `output_guidance` (~3–6 tasks, scaffold + ≤5 features) | trivial |
| 6 | Specialized roles: the coalescing agent (C compiler) | Agentic reporter + `grep`/`glob` + seam-hunting rule in `reporter.txt` | small |
| 7 | Design test output for the model (C compiler) | Counts-first in `<test_report>` **landed**; JUnit path + fixer read-loop **deferred** (fixer still one-shot; 6KB clip stays) | small |
| 8 | Route by difficulty, not only by role (Effective agents) | Last fix attempt: `fixer.run(escalate=…)` → `strongest_route()` (meta/Spark) | small |
| 9 | Dual-track: discovery before delivery (OpenCode plan/explore) | Requirements research pass → `docs/discovery.md` (five fixed `##` headings); `_readiness_gate` before planning (unanswered → `unresolved`; plan approval remains the hard gate) | medium |
| 10 | Product decisions via `ask_user`, not silent downgrade | For briefs that mention accounts / UI / integration: auth, embed method, and persistence are **mandatory** (research or ask); any demo/stub downgrade recorded in `assumptions` / `docs/plan.md` | small |
| 11 | Design brief before parallel workers (C-compiler design critic, properly) | Planner-owned: scaffold task owns `docs/design.md`; workers bound by it; serialisation via scaffold dependencies (no separate designer role) | medium |
| 12 | Stronger oracle than mechanical string checks | Prompt-level only in `requirements.txt` + test-author floor rules; Playwright/browser smoke **declined** (hermetic suite) | medium |
| 13 | Independent brief-reviewer (Effective agents evaluator-optimizer; OpenCode plan/explore review) | `REVIEWER` (Remy): read-only; runs only after suite green; evidence-backed gaps → one bounded `_test_and_fix(brief_gaps=…)` | medium |
| — | micro-fix | duplicate `status(TEST_AUTHOR, …)` line | trivial |
| — | micro-fix | `blame_tasks` falls back blindly to `graph.tasks[0]` when nothing matches | trivial |

Items 1–8 improve **reliability and token economics** (assembler quality).
Items 9–13 improve **product quality** (stop green-but-generic apps). Both
are landed (with the item 7 / 12 deltas above). A yes-app re-run
(`evals/results/yes-app-v3-2026-11-02.md`) still left Excalidraw unmounted
and the landing generic — closed by the **ui_product** skill follow-up
below; re-run `evals/prompts/yes-app.md` again to validate.

### Implementation status (2026-11-02)

Items 1–8 and both micro-fixes are landed, and so are items 9–13. Where the
implementation differs from the original plan:

- **Item 1** — the suite runs after every layer *but the last* (the fix loop's
  first run is that layer's CI run, so nothing runs twice), each per-layer run
  emits its own `test_run_finished`, and the green list travels to the next
  layer as a `<test_state>` block (greens, failures-still-landing, regressions).
  `smoke_check` now **raises** when a command fails
  (`executor._smoke_check`); the orchestrator treats a denial (`ToolDenied`)
  as advisory and any other failure as a broken layer (`LoopError`).
  `install_deps` runs right after the scaffold layer (host mode; docker still
  vendors at the end). The node `tsc` smoke command waits for `node_modules` —
  a missing install is not a broken file.
- **Item 2** — landed as planned: `fixer.run(history=…, known_gaps=…)` feeds
  `<previous_attempts>` (with a "do not repeat it" line) and `<known_gaps>`
  from the blamed workers' own `WorkerResult`s.
- **Item 3** — the reporter was already paths-only (corrected in Source 2), so
  the one model-facing leak fixed is `_run_layer`: agentic workers now receive
  dependency **paths** (`<dependency_files>`) instead of inlined contents. The
  re-read in `run_agentic` stays: it is Python-side (zero model tokens) and is
  the check that the writes actually landed.
- **Item 4 / 5** — landed as planned: `worker.txt` rule 5 (run the suite
  before `submit_result`, honest `known_gaps` on failure); planner sizing
  reconciled in **both** `planner.txt` rule 4 and `output_guidance`
  (`3-8 tasks` became `roughly 3-6, scaffold + at most five features`).
- **Item 6** — grep/glob only do something on the tool loop, so the reporter
  gained an agentic read-only pass (`reporter.run_agentic`, finish =
  `ReportBundle`, orchestrator switches on `agentic_devs`; one-shot
  `reporter.run` stays as the fallback). `submit_result`'s schema is
  overridden per call via `specs_for(overrides=…, ReportBundle)`. Seam-hunting
  rule is in `reporter.txt`.
- **Item 7** — counts-first landed (`fixer._summary_line`, first line of
  `<test_report>`). **Closed:** agentic `fixer.run_agentic` offers `FIX_TOOLS`,
  points at the JUnit path (`kiln-results.xml`), and the orchestrator uses it
  when `agentic_devs` is on; one-shot `fixer.run` remains the fallback.
- **Item 8** — `strongest_route()` in `routing.py` is the meta/Spark route the
  planning roles use; `Gateway.complete(route=…)` overrides per call;
  `fixer.run(escalate=…)` is set when `attempts + 1 >= max_attempts`, i.e. the
  last attempt the run can afford. The old cap status said "escalating" while
  doing nothing — it now reports "not green".
- **Item 9** — requirements research pass, not a new discovery role (chosen at
  implementation time): `requirements.txt` mandates research via
  `web_search`/`web_fetch` and a `docs/discovery.md` written under five fixed
  `##` headings (Auth model, Persistence, Integration method, UI direction,
  Stack choice); requirements gained `write_file` (docs/ only) and its clarify
  loop runs up to 4 tool turns (`MAX_TOOL_ROUNDS`). The orchestrator's
  `_readiness_gate` runs before planning: complete discovery → silent;
  missing sections → one `ask_user` round with the answers written back into
  the file, anything unanswered recorded as `unresolved` — a run never dies
  on a doc gap, and plan approval stays the hard gate.
- **Item 10** — landed in prompts: `requirements.txt` makes the auth /
  integration / persistence decisions **mandatory** for briefs that mention
  accounts, a UI, or an integration (resolve from research or ask_user, never
  a silent default), forbids silent downgrades (a demo instead of the real
  thing must be recorded in `assumptions` for plan approval), and
  `planner.txt` rule 1 forbids quietly dropping a criterion — an unhonorable
  one goes into `docs/plan.md` where the approver sees it.
- **Item 11** — planner-owned design, not a designer role (chosen): on UI
  briefs the **scaffold task** owns `docs/design.md` (tokens, layout, landing
  composition) — `planner.txt` rule 9 plus `output_guidance`; `worker.txt`
  rule 2 makes the design binding (no second palette or landing); the
  `DESIGN_DOC` constant lives in `planner.py`. Serial design falls out of the
  layering (design lands in layer 0; feature tasks depend on the scaffold),
  so `dependency_layers` itself is unchanged — the planner expresses the
  serialisation through dependencies.
- **Item 12** — prompt-level oracle in the hermetic suite; Playwright inside
  Docker remains declined. Host-only alternative: test_author (or a
  heuristic fallback) writes `docs/browser-flows.json`; after green +
  `served_ui_audit` + `runtime_smoke`, companion `browser_verify` runs a
  structured FlowPlan in headless Chromium (`persist=false`). Failures
  become `brief_gaps`. `requirements.txt` rule 2 and test-author floor
  rules still require template-failing oracles in unit tests.
- **Item 13** — new `REVIEWER` role, read-only (read/list/glob/grep +
  `submit_result`; `role_rules()` denies edit/shell/web), routed on the Spark
  route (reasoning, not coding), seated on the floor as Remy Reviewer.
  One-shot `reviewer.run` injects a capped workspace snapshot (25 files ×
  4KB); agentic `reviewer.run_agentic` reads the workspace itself — the
  orchestrator picks on `agentic_devs`, like the reporter. The verdict is a
  `ReviewVerdict`: evidence-backed gaps only, an empty list valid. The gate
  (`Orchestrator._review`) runs **only after the suite goes green**; gaps
  reopen the fixer for one bounded `_test_and_fix(brief_gaps=…)` round (its
  first green report does not count — the suite passes but the brief did not,
  so the fixer patches before green is accepted; the fix cap still applies
  inside), and the verdict reaches the reporter as a `<review>` block. Not
  green → no review.

### Follow-up — ui_product skill (2026-11-02, after yes-app-v3)

yes-app-v3 was green with real auth/SQLite/design tokens, but the served UI
still stubbed Excalidraw (JSON textarea) and `docs/design.md` prescribed a
generic indigo/system-ui look. The reviewer returned clear; the reporter
caught the seam too late.

**Landed:** `agents/skills/ui_product.txt` (distilled structure from
elayadesign/ai-design-skills MIT; Kiln-owned text). `needs_ui_product_skill`
gates attachment; `prod_ops` / `security` skills follow the same pattern.
Orchestrator sets flags after discovery+spec. Post-green mechanical
`served_ui_audit` + host `runtime_smoke` + host `browser_verify` (FlowPlan)
reopen the fixer on mount/stub/click-through gaps before the brief-reviewer.
Agentic fixer + post-edit diagnostics close item 7. On-demand `skill` tool +
`AGENTS.md` + `evals/checklist.py` land the thin P2/P1 eval gate. Floor UI
shows dynamic worker seats and an ask_user question dialog (dual-channel with
the companion terminal). Re-run yes-app to validate.

## Already in place — validated, no change needed

Kiln's shape matches the published architectures; these are claims we can make,
not work items (distinct from the numbered build-order items above).

| Method (source) | What Kiln already does |
| --- | --- |
| Orchestrator-workers + evaluator-optimizer (Effective agents) | `_phases`: requirements (+ discovery) → planner → parallel worker layers → test/fix → brief-reviewer (when green) → reporter. The suite is the mechanical evaluator; items 12–13 raise product-level strength. |
| Teach the orchestrator to delegate: objective, format, boundaries (Research system) | `planner.txt` demands self-contained descriptions ("a worker receives only its own task plus the contract"); the output format is schema-enforced (`TaskGraph`); boundaries are `owned_paths`, validated for overlap at plan time (`tasks.py:55-86`) and enforced at runtime by companion leases. Stronger than the source's prompt-only fix. |
| Task locking so claimants cannot collide (C compiler) | Three layers: plan-time (`TaskGraph` validator rejects overlapping `owned_paths`), runtime (`claim_scope`/`release_scope` leases), per-write (`may_write` + permission rules). The C-compiler project used a text-file lock in `current_tasks/` plus git synchronization to force the second claimant elsewhere (merge conflicts were the frequent *cost*, not the mechanism); Kiln refuses the overlap before work starts. |
| Structured handoff at the step cap, not a silent stop (Effective agents / OpenCode) | `loop.py` `HANDOFF`: tools withdrawn except `submit_result`; the agent must report accomplished / remaining / next step. Contract re-asserted at doom-loop refusals and at the cap. |
| Users of context: compaction-free bounded loops (Effective agents) | one conversation per task, hard `max_steps`, `MAX_TOOL_OUTPUT_CHARS` clipping, `reminder` at drift points — no unbounded history. |
| Independent units for parallelism (C compiler) | `dependency_layers` + disjoint ownership; a layer's tasks have no writes in common, so `asyncio.gather` cannot produce write collisions. |
| Tests as the independent judge (C compiler) | tests are written from the spec before any worker runs; workers and fixer are both denied `tests/*` writes (`deny_edit` in their rulesets) — the "never weaken a check" rule is enforced by permissions, not just by prompt. **Caveat:** independence ≠ strength. Item 12 raises oracle quality in the spec; item 13 reviews against the brief after green. |

## Source 1 — *Building effective agents*

Historical research notes; status of each gap is called out.

**Pattern vocabulary.** Kiln is canonically an orchestrator-workers workflow
(planner → workers) wrapped around an evaluator-optimizer loop (runner → fixer),
now also with a brief-reviewer gate after green. Worth one paragraph in the
writeup so the architecture can be defended by name.

**Ground truth at every step.** Agents should confirm progress from the
environment, not from their own beliefs. *Was:* `worker.txt` said "claim
nothing you have not verified" without requiring execution. **Landed (item
4):** rule 5 — run the relevant tests before `submit_result`; never submit on
assertion; honest `known_gaps` on failure.

**Agent-computer interface over prompts.** The source spent more time on tools
than prompts; their SWE-bench fix was switching to absolute paths once agents
could `cd`. *Kiln position:* paths are validated relative to the workspace root
(`safe_relative_path`, `workdir` inside the workspace) and workers do have
`shell`, so the same hazard exists. Standing guidance when touching tools:
every path-taking description in `tools/schemas.py` should state "relative to
the project root, whatever your current directory is", and every companion
refusal should teach the fix (which task owns the path, use `list_peers`,
record a `known_gap`). `loop.py` already returns errors as ordinary tool
output — keep every error string actionable.

**Routing by difficulty.** Role-based routing alone burns the last fix attempt
on the same model. **Landed (item 8):** on the last attempt,
`fixer.run(escalate=True)` uses `strongest_route()` — the meta/Spark route
planning roles already use, bypassing the open-weight fallback.

**Voting** (run the same task N times, aggregate): deliberately not taken —
see scope guards.

## Source 2 — *How we built our multi-agent research system*

**Scale effort to complexity.** Caps used to be static and the two planner
surfaces disagreed (`output_guidance` said 3–8 tasks; `planner.txt` capped
scaffold + five features). **Landed (item 5):** both reconciled to roughly
3–6 tasks (scaffold + at most five features), sized to the spec, each task
fitting one worker pass (30 steps).

**Artifacts through the filesystem, not the coordinator** ("game of
telephone"). Subagents should write files and pass lightweight references.
**Landed (item 3):** agentic `_run_layer` passes dependency **paths**
(`<dependency_files>`) instead of inlined contents. The reporter already
serialized paths/summaries only. The `worker.run_agentic` re-read stays —
Python-side, zero model tokens, write-verification. One-shot `worker.run`
still inlines contents (no tools).

**Synchronous layer barriers.** The source admits the lead blocks on the whole
subagent batch; Kiln's `_run_layer` gathers a whole layer before the next starts,
so one slow worker holds its layer (bounded by `max_steps`). Accepted for now;
the upgrade path is per-task readiness scheduling. Not in the build order.

**Token economics.** Multi-agent systems burn ~15× chat tokens; token usage
explains most performance variance. Kiln already counts per call with
`agent_id` and has run/project ceilings (`config.py`). The eval table should
record per-phase tokens (requirements / planner / tests / workers / fix loop)
from the gateway usage log — data exists, the aggregation is the work.

**Prompt iteration levers.** Simulate, trace, read transcripts, fix failure
modes. `TranscriptLog` + the gateway's `_record_transcript` already capture the
raw material; the eval harness should read it (per `agent-patterns.md` item 7).

## Source 3 — *Building a C compiler with a team of parallel Claudes*

**CI after every increment.** *Was:* the suite ran once after all layers.
**Landed (item 1):** `install_deps` after scaffold (host mode); suite after
each layer but the last; `<test_state>` (greens / still-landing / regressions)
to the next layer. Per-layer runs are regression checks — a red suite after
layer 1 is expected; import/syntax failures fail the layer via hard
`smoke_check`. `ToolDenied` on smoke stays advisory. `run_tests` approvals
use a `remember_key`, so one approval covers later identical commands.

**Known-good oracle.** Their GCC-as-oracle let agents bisect failures against a
reference that was true regardless of local state. *Kiln analogue:* early
per-layer green lists + smoke as the contract to keep. Subsumed by item 1.

**Design test output for the model.** **Partial (item 7):** aggregate counts
land first in `<test_report>` (`fixer._summary_line`). The JUnit file still
lands at a fixed workspace path, but the fixer remains one-shot (`FIX_TOOLS`
declared, never offered), so the path/loop upgrade is deferred and the 6KB
clip stays. Sampled suite mode for golden-prompt runs is still open.

**A running doc of failed approaches.** *Was:* attempts tracked as an integer
only; diagnoses and `known_gaps` never reached the fixer. **Landed (item 2):**
`fixer.run(history=…, known_gaps=…)` feeds `<previous_attempts>` and
`<known_gaps>` from blamed workers.

**Fresh-session orientation.** Each agent starts cold; their fix was a ritual
(read the progress files, orient, then act). *Kiln position:* workers get the
contract and `<test_state>`, and `docs/design.md` is binding when present, but
there is still no explicit "orient first: `list_dir` owned paths + re-read
`docs/contract.md` from disk" rule. **Still open** (not in items 1–13).

**Specialized roles.** **Landed (item 6):** agentic reporter with `grep`/`glob`
and a seam-hunting rule (duplicates, stubs, contract routes with no
implementation). The C-compiler **design critic** is *not* that reporter rule —
product/UI critique is items 11 and 13 (design brief + brief-reviewer).

**Parallelism needs independent units.** Oversized tasks are constrained by
item 5’s sizing; `HANDOFF` carries `known_gaps`. For UI apps, design-critical
work serialises through the scaffold task owning `docs/design.md` and feature
dependencies on it (item 11) — without changing `dependency_layers` itself.

## Product quality gap (yes-app / UI briefs)

Anthropic’s compiler and research systems assume a **strong external oracle**
(GCC behaviour, search quality metrics). Kiln’s suite is **author-written from
the spec**. The yes-app run showed that under the *pre-fix* prompts the suite
was under-specified for product briefs. Items 9–13 address that in the
harness; product quality still needs a re-run of `evals/prompts/yes-app.md`
to prove out.

**Postmortem — the yes-app run (2026-11-02), from one typed prompt:**

> a note taking app with clean UI, excalidraw integrated, user accounts,
> login/sign up and landing page aswell

Evidence: that prompt, the app the run produced, and the approval questions
the pipeline put to the user mid-flight. Outcome: ~427k tokens, 0 fix rounds,
vitest green — yet: system-ui CSS, client-only `localStorage` "accounts",
Excalidraw via CDN + canvas fallback, landing that passes "has `<h1>` /
mentions note". The harness optimised for **tests green**, not for the brief.
The prompt is recorded at `evals/prompts/yes-app.md` so later runs can
reproduce the comparison; cost and wall clock were not captured.

**Root causes in the pre-fix prompts/contracts (addressed by items 9–13
except where noted):**

1. Requirements steered toward a boring first shell and left out auth
   providers unless the idea *was* auth — product claims got silently
   downgraded. → **Item 10** (mandatory product decisions + recorded
   downgrades). The "keep scope tight / leave out auth providers" tension
   remains by design; silence is what was forbidden.
2. Stack collapsed to `python` | `node` Express + vanilla + localStorage for
   UI-heavy ideas. → Partially mitigated by **item 9** (Stack choice in
   discovery); stack_id enum itself is unchanged.
3. The oracle is authored by requirements and its *strength* was never
   checked — a trivial oracle yielded a trivial test. → **Item 12**
   (prompt-level oracle quality; Playwright declined).
4. No discovery / design / brief-review — pipeline jumped to scaffold and
   parallel feature work. → **Items 9, 11, 13**.
5. (inferred from the outcome, not read off the prompts) Shared UI files got
   one owner mid-fan-out; "clean UI" became a thin CSS file. → **Item 11**
   (scaffold-owned design, binding on workers).

**What items 9–13 close:**

| Item | Failure mode it closes | Status |
| --- | --- | --- |
| 9 Discovery | Integrating Excalidraw / choosing auth without research; scaffold before readiness | Landed (soft readiness; plan approval hard) |
| 10 Product-shape decisions | Silent localStorage / CDN / demo-auth without a recorded decision | Landed |
| 11 Design brief + serial scaffold | Generic system CSS and competing landing invents from parallel workers | Landed (no separate designer role) |
| 12 Stronger oracle | Green suite that a brochure template also passes | Landed (prompts only; no Playwright) |
| 13 Brief-reviewer | Author grading itself; no check against the original user idea | Landed (after green only) |

**Plan readiness checklist** (filled in `docs/discovery.md`; `_readiness_gate`
asks once for missing sections, else records `unresolved` — does **not** kill
the run):

- Auth model (real sessions vs explicit demo)
- Persistence (server DB vs local-only, and stated as such)
- Third-party integration method (official embed/npm vs CDN vs stub)
- UI direction (palette, type, layout — at least `docs/design.md` bullets from
  the scaffold task)
- Stack choice justified by discovery (not defaulted solely because `node`)

**Relationship to items 1–8:** per-layer CI and “run tests before submit”
make a weak suite fail faster and more honestly; they do not make the product
good. Both tracks are landed; use the yes-app golden prompt to measure whether
product output improved.

## Deliberately not taken (scope guards)

- **Voting / multi-shot confidence** (Effective agents): run the same task
  several times and aggregate. Multiplies the 15× token burn for marginal gain
  at demo scale.
- **Per-task readiness scheduling** replacing the layer barrier: real
  complexity (result coordination, partial-layer failures) for a bounded wait.
- **Infinite generation loop** (C-compiler harness): Kiln ends honestly at the
  fix cap instead of looping forever.
- **Child-session resumability and rainbow deployments** (research-system
  post): resumability is also noted in `agent-patterns.md`; rainbow
  deployments appears only in that post. Neither is taken — the per-run
  workspace and event log remain enough for demo scale.
- **Full end-state / LLM-judge evals as the only gate** (Research system):
  deferred to the eval harness work in `agent-patterns.md` items 7–8. Item 13’s
  brief-reviewer is a **run-time** gate with evidence-backed gaps, not a
  substitute for that harness.
- **Playwright inside the hermetic Docker suite** (was optional under item
  12): declined — browser downloads and a started server fight the
  no-network suite. **Alternative landed:** mechanical `served_ui_audit`,
  host-only `runtime_smoke` (HTTP GET /), then host-only `browser_verify`
  (structured FlowPlan / Playwright Chromium, not free-form MCP) after suite
  green; gaps reopen the fixer like brief gaps. Free-form Playwright MCP in
  the worker loop remains a non-goal.
- **Fixer read-loop + JUnit path** (remainder of item 7): **landed** via
  agentic fixer.
- **Dedicated discovery or designer roles**: folded into requirements research
  and the scaffold task instead.
- **Fresh-session orientation ritual** (Source 3): still open — not in
  items 1–13.

In the build order and landed (with deltas above): discovery, mandatory
product `ask_user`, design brief, stronger prompt oracles, independent
brief-reviewer (items 9–13).

## Notes against the current tree (2026-11-02)

Facts the Implementation status assumes:

- Prompts load through `system_prompt_for(Role)` (`definitions.py`), which
  appends a per-role `output_guidance` soft cap. Prompt-level changes land in
  either the `.txt` template or `output_guidance`, not in ad-hoc string
  concatenation.
- Per-role `max_output_tokens` is gone; config defaults to `None` (no ceiling).
  `Gateway.complete` detects truncation and empty content explicitly.
- The shared tool loop lives in `agents/loop.py` (step-cap handoff, doom-loop
  guard, contract reminders). Workers use it; agentic reporter and reviewer
  also ride the same loop pattern. The planner's exploration
  (`planner.py`) and the one-shot fixer have their own paths.
- `run_tests` / `smoke_check` approvals are remembered per run (`run_tests` by
  command key, `smoke_check` by `smoke:{stack_id}`), so per-layer test runs do
  not re-prompt for the identical command.
- The JUnit report file is written to a fixed workspace path before parsing;
  item 7’s counts-first summary is in the fixer message; passing that path into
  a tool-using fixer is deferred.
- Node/vitest is a first-class stack (`Spec.stack_id`, `test_author_node.txt`);
  anything touching tests goes through `profile.test_command`. Playwright in
  Docker stays declined; host `browser_verify` is the click-through gate.
- Requirements research uses `web_search` / `web_fetch` (approval-gated) and
  writes `docs/discovery.md` before the spec; `_readiness_gate` runs before
  planning.
- Product-shape questions (auth / integration / persistence) are **mandatory**
  when the brief mentions accounts, a UI, or an integration; discretionary
  asks remain for other stack/shape clarifications. Scope downgrades must be
  recorded in `assumptions` (and visible at plan approval).

## Attribution

Design methods were studied from the three public Anthropic engineering posts
named above, plus OpenCode plan/explore patterns, Cursor/Claude verification
practice, and dual-track product-management practice for items 9–13. No code
was borrowed; every "lands in" target is a Kiln file and every change is
written fresh for this hackathon, consistent with the attribution rule in
`README.md`.
