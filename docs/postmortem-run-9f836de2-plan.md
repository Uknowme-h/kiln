# Postmortem run-9f836de2 — root causes and the fix plan

Status: analysis done 2026-11-03. Evidence: `.kiln/runs/run-9f836de2/`
(`events.jsonl`, `summary.json`, `transcript.jsonl`), the generated app at
`D:\Projects\AIChatbot`, and the Kiln source under `src/kiln/`.

This document corrects two misdiagnoses in the raw postmortem, pins every
failure to the exact code that caused it, maps each fix to how the mature
coding agents (OpenCode, Aider, OpenHands, SWE-agent, Claude Code) already
solve the same problem, and lands the work in a build order that dovetails
with `docs/production-readiness-plan.md`. Budget mechanics are out of scope
here per request — only the *engineering* failures.

---

## TL;DR — what actually happened

The suite went 17/17 green in ~50 minutes and the run was still recorded
**failed**. Three independent defects stacked; any one of them alone would
have sunk or cheapened the run.

| # | Failure | Real root cause (file:line) | Severity |
|---|---------|------------------------------|----------|
| 1 | Run killed after green suite | Orchestrator writes `README.md` under the **reporter's** lease, but the reporter never read that file → `ReadLedger.check` refuses → unhandled `ExecutionError` crashes `_phases` | **fatal, mechanical** |
| 2 | Fixer no-op'd on real UI break | Reviewer gaps reach the fixer as advisory `known_gaps`, but the fixer's only framing is "the suite is failing" and the code *explicitly blesses an empty patch when the suite is green* | **high, structural** |
| 3 | App shell shipped unmounted | No gate checks that the served UI mounts the app. `runtime_smoke` GETs only `/`; `browser_verify` silently returns `[]` when Playwright/Chromium is absent; `served_ui_audit` has no rule for "entry doesn't mount the router" | **high, verification gap** |

Two of the postmortem's "mistakes" are **not what they looked like** — see
Corrections below; treating them as the postmortem framed them would waste
the fix effort.

---

## Corrections to the raw postmortem

Read the evidence before accepting the blame assignment. Two entries are
misattributed, and fixing the wrong thing is the most expensive outcome.

### 1. "Reporter tripped a read-before-write guard" — wrong owner. It's an orchestrator bug.

The reporter agent behaved **correctly**. Transcript `seq 1237–1242`:

```
seq 1238  reporter submit_result → "accepted"     (the bundle validated)
seq 1240  reporter claim_scope    → leased README.md, AGENTS.md, docs/
seq 1241  reporter write_file README.md
seq 1242  write_file ✗ ExecutionError: read README.md with read_file before you change it
```

The reporter loop has **read-only tools** (`agents/reporter.py:190` →
`("read_file", "glob", "grep", "submit_result")`). It *cannot* write the
README. The write is done by the **orchestrator** in `_phases`
(`graph/orchestrator.py:715` `self.write_files(..., agent=reporter_scope)`),
which drives `write_file` *under the reporter's agent identity*. The
companion's `_write_file` (`companion/executor.py:456`) then runs
`self.reads.check("reporter", "README.md", current)` and refuses because the
reporter never recorded a read of the old README.

The reporter *did* read `frontend/src/main.jsx`, `frontend/src/App.jsx`,
`backend/app.py`, `backend/embed.py` … to verify claims — but had no reason
to read the README it was about to *replace*. So the guard fired on the
orchestrator's own write, attributed to the reporter. **The fix belongs in
the orchestrator / ledger, not in the reporter's prompt.**

### 2. "Duplicate tool invocations doubled the context" — mostly a telemetry artifact, not model behavior.

The postmortem counted "identical tool calls in pairs at the same timestamp"
and concluded the model was calling tools twice. The transcript shows the
opposite: each *real* call is recorded **twice** because two different layers
both log it.

- `agents/loop.py:280` (`run_agent_loop.handle`) records `tool_call`.
- `graph/orchestrator.py:226` (`Orchestrator.call_tool`) records `tool_call`
  **again** for the same call.

Confirmed in the data: `seq 1152` and `seq 1153` are the *same* fixer
`read_file kiln-results.xml` call logged twice with identical args and
near-identical timestamps. Across the run there are 538 `tool_call` records
and 204 exact-duplicate (agent, name, args) groups — the double-logging.
The model's genuine parallelism (e.g. the reviewer issuing `read_file,
read_file, read_file, read_file` in one turn) is **four different files in
one batch**, which is efficient, not a defect.

**Do not** build "deduplicate the model's tool calls" — the model is fine.
Fix the double-logging so the transcript (and any token accounting derived
from it) stops overstating tool traffic. The *prompt-bloat* problem is real
(see Failure 4), but it is caused by context design, not duplicate calls.

---

## Failure 1 — run killed by a tool-rule the agent never broke (fatal)

### Root cause

`graph/orchestrator.py` `_phases`:

```python
await self.call_tool(run_id, "claim_scope", {...}, agent=reporter_scope)   # 698
report_files = [WorkFile("README.md", bundle.readme), ...]                 # 708
await self.write_files(run_id, report_files, agent=reporter_scope)         # 715
```

`write_files` (`orchestrator.py:237`) issues `write_file` as the reporter.
`companion/executor.py:_write_file` enforces the `ReadLedger`
(`companion/leases.py:129`): overwrite requires a prior `read_file` **by the
same agent id**. The reporter never read `README.md`, so
`ReadLedger.check` (`leases.py:145`) returns
`"read README.md with read_file before you change it"`, the executor raises
`ExecutionError`, and it propagates out of `_phases` → `run()` returns
not-green → `run_finished green:false`.

The `ReadLedger` exists for a good reason (prevent two concurrent workers
silently clobbering a shared file). But the reporter is a **single-threaded,
terminal** writer, and the write is initiated by the orchestrator, not by an
agent that might be racing a peer. The guard is correct for workers and
wrong here.

### How others solve it

- **Aider** tracks which files are in the chat and refuses to apply an edit
  to a file it hasn't been shown — but it *auto-reads* the file into context
  on the first edit attempt rather than hard-failing, and its docs-writer
  path reads-then-writes in one step. The guard is a speed bump, not a wall.
- **OpenCode** enforces permissions (`edit <path> allow/ask/deny`) as a
  *policy* layer, but the orchestrating session that owns a document writes
  it directly; a subagent that *drafts* content returns the text and the
  parent writes it. Ownership of the write is separated from ownership of
  the content.
- **OpenHands** separates the *agent* (produces a `write` action) from the
  *runtime* (executes it). The runtime doesn't require the agent to have
  read a file it's deliberately overwriting in a final "publish" step.

### Fix (Kiln)

The cleanest, smallest change that keeps the guard where it matters:

1. **Write final documents under an orchestrator-owned scope, not the
   reporter's.** The reporter *drafts* (returns the `ReportBundle`); the
   orchestrator *publishes*. Create a dedicated `"orchestrator"` scope for
   the publish step and have it pre-read each target before writing, which
   satisfies the ledger honestly.

2. **Make `write_files` ledger-safe.** Before each `write_file`, if the
   target exists, issue a `read_file` for it **under the same agent scope**
   so the ledger records the read. This is a two-line change in
   `write_files` and fixes the whole class of bug, not just the reporter:

   ```python
   async def write_files(self, run_id, files, agent=None):
       for file in files:
           # satisfy the read-before-write ledger for the identity we write as
           await self.call_tool(run_id, "read_file", {"path": file.path},
                                agent=agent)  # best-effort; absence is fine
           await self.call_tool(run_id, "write_file",
                                {"path": file.path, "content": file.content},
                                agent=agent)
   ```

   (The read must tolerate "no such file" — a fresh README shouldn't fail.
   `read_workspace` already swallows that; mirror it.)

3. **Never let a docs/publish step flip a green run to failed.** Wrap the
   reporter publish in a guard: on `ExecutionError`, log a structured
   warning and record the run as `green` with a `docs_error` annotation, so
   a documentation I/O hiccup cannot erase 50 minutes of verified work. A
   missing README is a *defect to report*, not a *run failure*.

---

## Failure 2 — fixer treated reviewer gaps as optional (high)

### Root cause

The data flow is correct; the *contract* is broken.

`_phases` (`orchestrator.py:648`) does the right thing: reviewer gaps become
`brief_gaps` and reopen `_test_and_fix`. Inside, `known_gaps` leads with the
brief gaps (`orchestrator.py:877`). So the fixer *receives* "App never
mounts; no /app route".

But three things let it rationalize doing nothing:

1. **The prompt frames only failing tests.** `agents/fixer.py` `run_agentic`
   opens with *"The suite is failing. Use your tools to read the JUnit
   report…"* and the system prompt `fixer.txt` is entirely about turning red
   tests green. The reviewer gaps sit in a subordinate `<known_gaps>` block
   with no instruction weight.

2. **The empty patch is explicitly blessed on green.**
   `agents/fixer.py:376`:

   ```python
   if report.green and not outcome.result.files_written:
       return FixPatch(... files: [] ...)   # lets the fix loop exit cleanly
   ```

   This escape hatch was written for "a product-gate that was really host
   env". It is *too* broad: it also fires when there are real, code-fixable
   reviewer gaps. The fixer read `kiln-results.xml` (seq 1152), saw 17/17,
   and — correctly per its framing — submitted *"Suite already green … No AC
   check failing"* with `files_written: []`. The escape hatch accepted it.

3. **`fixer.txt` line 38–40 actively tells it to stand down on green:**
   *"If the suite is already green and the brief gap is host/tooling (not a
   code defect), say so in diagnosis and leave files_written empty."* The
   model has to decide "host/tooling vs code defect" with no help — and the
   gaps it was handed (unmounted App, missing /app route) are unambiguously
   code defects, but the prompt gives that distinction no teeth.

The postmortem's "fixer only read kiln-results.xml, patched nothing" is the
*symptom*. The cause is that Kiln hands the fixer evidence-backed product
gaps but tells it to act only on failing tests, then rewards inaction when
the suite happens to be green.

### How others solve it

- **SWE-agent** separates the *issue* from the *test*. The agent's goal is to
  resolve the issue; the test suite is only a *signal*. It never treats
  "tests pass" as "issue resolved" — the loop ends on an explicit submission
  judged against the issue, not on green.
- **OpenHands / Claude Code** run a *verification* step whose failure modes
  are first-class work items. A failed verification creates a new task with
  the same urgency as a failing test; there is no cheaper path that skips it.
- **Aider's `/test` + `/lint` loop** feeds the *failing output* back as the
  next instruction regardless of whether it's a test, a linter, or a type
  error — the channel is "here is evidence of a defect", not "here is a red
  test".

The common principle: **"green tests" and "brief is met" are two separate
oracles, and a gap from either is mandatory work with the same mechanics.**

### Fix (Kiln)

1. **Make reviewer/brief gaps a first-class fixer trigger, not a footnote.**
   When `brief_gaps` is non-empty, the fixer prompt must lead with them as
   *the* work item. Change the opening line conditionally:

   ```python
   if brief_gaps:
       opening = ("The suite is green, but the app does not yet deliver the "
                  "brief. These evidence-backed gaps are the work — patch the "
                  "code so each is resolved:")
   else:
       opening = "The suite is failing. Use your tools to read the JUnit report…"
   ```

2. **Narrow the green-suite escape hatch to host-env only.** The
   `report.green and not files_written` early-return in `fixer.py:376`
   should only apply when there are *no* actionable brief gaps. Pass a flag
   (`has_actionable_gaps`) and require a non-empty patch when it's true:

   ```python
   if report.green and not outcome.result.files_written and not has_actionable_gaps:
       return FixPatch(... empty ...)
   ```

   Add a `check` that rejects an empty `files_written` when `brief_gaps`
   were provided, with the complaint: *"reviewer filed N code gaps — an
   empty patch is not acceptable; patch the files or explain in diagnosis
   why each gap is host/tooling, not code."*

3. **Teach the host-vs-code distinction.** In `fixer.txt`, replace the soft
   line 38–40 with a decision rule: a gap naming a file under the workspace
   (a route not mounted, an entry not importing a module, a missing build
   output the code should produce) is a **code defect** and demands a patch;
   only "Playwright/Chromium not installed", "no start command", port
   conflicts, etc. are host/tooling and may be left empty.

4. **Cap and verify the re-review.** After a brief-gap fix round, re-run the
   reviewer (or at minimum re-run the product gates) before declaring green,
   so the fixer's patch is itself checked. Today the loop trusts one fixer
   round and never re-verifies the gaps it was handed.

---

## Failure 3 — the served UI was never actually verified (high)

### Root cause

The run had *three* product gates, and all three had a hole exactly where
this bug lived:

- **`runtime_smoke`** (`product/runtime_smoke.py:39`) GETs only `paths or
  ["/"]`. `/` returned the Landing page (200), so smoke passed. It never
  GETs `/app`.
- **`browser_verify`** (`product/browser_verify.py:87`) returns `[]` (pass)
  when Playwright isn't importable, and again when Chromium isn't installed
  (line 111). The events log shows *no* browser/gate events between green
  (09:54:55) and reviewer start (09:55:05) — a 10-second gap — meaning
  `_product_gates` ran and produced nothing actionable, i.e. the browser
  path silently skipped. This is precisely the "browser setup can be
  silently skipped" risk the production-readiness plan already flags (Phase
  5.2).
- **`served_ui_audit`** (`product/served_ui_audit.py`) only checks *known
  third-party packages* (excalidraw, monaco, leaflet…) and stub-host
  patterns. It has no rule for "the SPA entry point doesn't mount the app
  router" or "the backend has no route for the app shell".

So the only thing that caught the real defect was the **reviewer agent**
reading the files by hand — a human-grade catch that the mechanical gates
should have made. The gates exist but verify the wrong surface.

### How others solve it

- **OpenHands / Claude Code** treat "does it run and render" as a required
  gate for any UI deliverable: launch the dev server, load the page in a
  headless browser, assert on a visible element and a clean console. A
  missing browser is a **blocked** status, never a silent pass.
- **SWE-bench-style harnesses** define *fail-to-pass* tests: checks that
  fail before the fix and must pass after. The analog here is a *product
  fail-to-pass gate*: "GET /app returns 200 and the served bundle mounts the
  router" — a check that is red before the fix and green after, so the fixer
  has an oracle.
- **Playwright-based E2E in CI** never treats "browser not installed" as
  success; `playwright install` is a provisioning step, and its absence
  fails the job loudly.

### Fix (Kiln)

1. **Make browser absence BLOCKED, not PASS** (production-readiness Phase
   5.2 — implement it). Introduce explicit gate statuses
   `PASS / FAIL / BLOCKED_HOST_SETUP / NOT_APPLICABLE`. For a `ui_product`
   run, `BLOCKED_HOST_SETUP` must prevent a green result unless the user
   explicitly accepts the limitation. This single change would have turned
   run-9f836de2's silent skip into a loud, actionable signal.

2. **Add a "served app mounts" audit rule.** Extend `served_ui_audit` (or a
   new `app_mount_audit`) for SPA stacks: parse the HTML entry's script tag
   → resolve the entry module → confirm it imports/renders the router/App
   component, and confirm the backend registers a route serving the app
   shell path. Concretely for this run it would flag: `frontend/src/main.jsx`
   renders `<Landing/>` for all paths and never imports `App.jsx`; `backend/
   app.py` has no `/app` route; `frontend/dist` absent. This is a static
   check — cheap, deterministic, no browser needed — and catches exactly
   Failure 3.

3. **Smoke the app route, not just `/`.** When the spec/plan names an app
   shell path (`/app`, `/dashboard`), `runtime_smoke` should GET those paths
   too. Source the paths from the contract routes rather than hardcoding
   `/`.

4. **Provision the browser** (production-readiness Phase 5.1): an
   `ensure_browser_runtime` that detects Playwright + Chromium, installs the
   pinned browser after approval, and verifies a headless launch. Without
   this, gates 1–3 are advisory forever on a fresh host.

---

## Failure 4 — worker prompt bloat (real, but not from duplicates)

### Root cause

3.27M worker prompt tokens across 117 calls; worker-5 alone 942k. The cause
is **context design**, not duplicate calls:

- Every worker turn re-sends the **full spec JSON + full inlined test-suite
  bodies + full contract JSON**. `agents/worker.py:_tests_block` inlines
  every test file up to 6000 chars *each*; `_contract_block` dumps the whole
  contract; the `<spec>` block is the entire spec. This is the per-turn
  floor.
- The loop only *appends*: every `read_file` result is added to history and
  re-sent on the next call. worker-5 did 60 `read_file` calls — each file
  body paid forward on every subsequent model call for the rest of the task.
- The `reminder` re-injects the **full contract JSON** on doom-loop refusal,
  step-cap handoff, and after compaction (`worker.py:337`,
  `loop.py:336`) — so drift-recovery is itself expensive.
- Compaction exists (`loop.py:maybe_compact`) but is gated at
  `compact_threshold_chars = 100_000` and capped at 2 times, and the fixer
  inlines `current_files` *in addition* to the model reading them.

### How others solve it

- **Aider's repo-map**: instead of inlining whole files, Aider sends a
  compact *map* (file list + key signatures, ranked by relevance) and reads
  full file bodies on demand. Context stays bounded by the map, not the
  corpus. Kiln's analog: send spec/contract as a *digest + path reference*,
  read full sections via tools.
- **OpenCode's compaction + pruning**: old tool results are aggressively
  rewritten to placeholders (which Kiln already does in `context.py`), *and*
  the objective/contract are re-injected compactly after compaction rather
  than on every drift event.
- **OpenHands' event stream**: condenses history when it grows, keeping the
  *most recent* observations verbatim and summarizing the rest; it never
  re-sends large static documents per turn.
- **Subagent offloading (all three)**: heavy search/read work is pushed to a
  read-only subagent that returns a *summary*, keeping the parent's context
  small. Kiln has `spawn_explore` (F1) but workers didn't use it — they read
  files directly into their own context.

### Fix (Kiln) — lands production-readiness Phase 2

1. **Stop inlining the test suite and full spec into every worker**
   (Phase 2.2). Send each worker only: its task, the AC ids it `covers`,
   the *paths* of relevant test files, and failing cases when fixing. Let it
   `read_file` the exact tests. Spec/contract become a digest + path
   reference, not a full dump.

2. **Bound the reminder.** Re-inject a *compact* contract digest (routes +
   owned paths), not the full pretty-printed JSON, on drift/compaction.

3. **Tool-output policies** (Phase 2.5): require ranges on large
   `read_file`, bound `grep` context, cap `list_dir`, offload large tool
   bodies to `.kiln/tool-out/` and return excerpts (the offload plumbing
   already exists per the plan's "recent changes").

4. **Make compaction proactive and measured.** Trigger on *estimated tokens*
   rather than raw chars (needs the Phase 1 estimator), before the request
   balloons, and record tokens saved. Only `worker.py` and `fixer.py` forward
   the tunable compaction settings into the loop today; `reviewer.py` and
   `reporter.py` fall through to the loop defaults (`enable_compaction=True`,
   `compact_threshold_chars=100_000`), which is serviceable but untuned —
   pass the settings through explicitly so all roles share one policy.

5. **Nudge workers to `spawn_explore` for multi-file discovery** so the
   parent context stays lean (the tool exists; the worker prompt should
   prefer it for "read N files to understand a seam").

---

## Failure 5 — 125/145 calls on the fallback route (medium, expected-config)

### Root cause

This one is **not a malfunction**. `gateway/routing.py:route_for`: coding
roles (`OPEN_WEIGHT_ROLES` — workers, fixer, reviewer) route to Muse Spark
Open **only when `open_weight_base_url` is configured**. It was not set for
this run, so every coding call fell back to the Meta Muse Spark route with
`fallback=True` (line 56). The planning roles (requirements, planner,
test-author, reporter) use Muse Spark directly (`fallback=False`), which is
why only 125 of 145 were fallback — the 20 non-fallback calls are exactly
the planning/reporter roles.

So "125 fallback" means "**the open-weight coding endpoint was never
configured for this run**", not "the primary path kept flaking". The cost is
real (Muse Spark is the more expensive path for the bulk coding work), but
it's a *configuration* gap, not a routing bug.

### Fix (Kiln)

1. **Surface it as a first-class run fact.** `/routing` already exposes
   `configured` and `fallback` per role. Emit a run event at start listing
   which roles are on fallback, and have the reporter state it in the cost
   report — "N% of coding calls used the fallback route because
   `open_weight_base_url` is unset" — so the user knows the lever.
2. **Decide the day-one comparison** (per `README.md`): the plan is already
   to compare tool-call reliability on Muse Spark Open and keep workers on
   it if it holds up. That's the real fix — configure the endpoint.

---

## Failure 6 — parallel workers raced on files that didn't exist yet (medium)

### Root cause

Workers run in dependency layers (`orchestrator.py:_run_layer`,
`dependency_layers`). Within a layer they're parallel by design. But:

- worker-3 (T2, bots) and worker-4 (T3, chat) hit `ExecutionError` reading
  `backend/auth.py` and `frontend/src/components` **before those files
  existed** — they were started while the peers that produce those files
  were still running, because the planner's `depends_on` graph didn't
  capture the real seam dependency (chat depends on auth; embed depends on
  components).
- A read of a not-yet-written file raises `ExecutionError` ("no such file")
  instead of returning a soft "not present yet" — so a speculative read is
  indistinguishable from a real error, and the worker burns steps recovering.
- worker-4 additionally looped on shell smoke tests against an auth cookie
  flow that wasn't ready.

The lease/`ReadLedger` model prevents *clobbering*, but nothing orders
*reads* against peer production, and nothing tells a worker "this dependency
is still landing".

### How others solve it

- **OpenCode subagents** run against a snapshot; a child reads what existed
  at spawn, never a peer's half-written file, and the parent merges. (Kiln
  deliberately didn't take child-session snapshots — noted in
  `agent-patterns.md` — so it needs a different answer.)
- **Build systems (the honest analog)**: a task's inputs are declared; the
  scheduler runs a task only when its declared inputs exist. Kiln's
  `depends_on` is that declaration, but the planner under-declared the
  edges, and the runtime doesn't *wait* on undeclared-but-needed files.

### Fix (Kiln)

1. **Soft-miss for reads of not-yet-existing peer files.** When `read_file`
  targets a path inside *another active agent's* lease and the file doesn't
  exist, return a soft signal — `"<path> does not exist yet (owned by
  <peer>, still being written)"` — instead of raising `ExecutionError`. This
  turns a crash-loop into information the worker can act on (wait, stub, or
  record a gap).

2. **Tighten `depends_on` inference in the planner prompt.** The planner
   already gets "add a feature→feature edge only when a real seam requires
   it" (`planner.txt:45`). Strengthen it: if task B's files import from or
   call into task A's owned paths, B **must** `depends_on` A — name the
   common seams (auth → chat/profile; components → embed; models → routers).

3. **Expose peer readiness via `list_peers`.** Workers already have
   `list_peers` ("status, files written so far"). Make the worker prompt use
   it to check whether a needed peer file exists before depending on it, and
   prefer coding against the *contract stub* when the peer hasn't landed.

---

## Cross-cutting — the two cheap, high-leverage hygiene fixes

These are small but remove whole classes of confusion:

1. **Fix the double-logging** (`loop.py:280` + `orchestrator.py:226`). Pick
   one recorder for `tool_call`/`tool_result` — the loop is the right owner
   (it sees local tools the orchestrator doesn't). Have `Orchestrator.call_tool`
   stop re-recording, or pass a flag. This makes the transcript truthful and
   stops token/tool analytics from overstating traffic by ~2× on
   orchestrated calls.

2. **Re-verify after the final fixer write** (production-readiness Phase 6).
   Today, after a brief-gap fix round, the loop trusts the fixer and never
   re-runs the reviewer or the product gates. Centralize the green
   invariant — suite green AND zero skips AND zero errors AND product gates
   passed AND brief review clear — and re-check it after *every* fixer
   write, so the last patch is itself verified.

---

## What went well (keep these)

The postmortem's "what went well" is accurate and worth protecting:

- **Tight AC-backed spec** with named oracles — this is why the suite was
  meaningful.
- **Clean 5-task split + contract** — parallel workers didn't rewrite each
  other; the leases held.
- **Test author**: 6 oracle files in one call, 15k tokens, fully green
  later — highest-leverage single call in the run.
- **worker-2** (auth+profile): 3.6 min, 14 calls, empty known_gaps — the
  efficiency model to clone.
- **The reviewer** did not rubber-stamp green tests — it caught the real
  product hole the mechanical gates missed. This is the behavior to make
  *systematic* (Failure 3), not remove.

---

## Recommended build order

Ordered by leverage: stop killing green runs first, then make verification
real, then cut cost. Items marked **[PRP-n]** implement phases of
`docs/production-readiness-plan.md`.

### Milestone A — Stop killing green runs (fatal-first) ✅ DONE

1. Fix the reporter/publish read-before-write crash: pre-read targets in
   `write_files`, publish docs under an orchestrator scope, and never let a
   docs I/O error flip a green run to failed. *(Failure 1)* — **done**:
   `write_files` pre-reads each existing target under the same scope; the
   reporter publish block is wrapped so any docs failure logs + marks the
   reporter stuck but returns the suite's `green` verdict; the scope lease is
   released in a `finally`.
2. Fix the transcript double-logging. *(Cross-cutting 1)* — **done**:
   `Orchestrator.call_tool` gained `record: bool = True`; the four
   `run_agent_loop`-fed closures (worker, fixer, reviewer, reporter) pass
   `record=False` so each call is logged once (by the loop), while
   orchestrator-driven calls (claim_scope, plan/requirements loops) still log.
3. Add a regression test: green suite + reviewer-gap + reporter publish →
   run is green, README written. — **done**: `test_a_docs_write_failure_does_not_flip_a_green_run`,
   `test_write_files_reads_each_target_before_overwriting`,
   `test_loop_driven_agent_calls_are_recorded_once` (all in `tests/test_orchestrator.py`).

### Milestone B — Make "green" mean "delivers the brief" ✅ DONE (4, 5, 6, 7)

4. Make reviewer/brief gaps mandatory fixer work: conditional opening,
   narrow the green escape hatch, reject empty patches when gaps exist.
   *(Failure 2)* — **done**: `fixer.run_agentic` frames green-suite brief work
   around the gaps (not the JUnit) via `green_suite_brief_work`; the `check`
   rejects an empty `files_written` when code gaps exist; the escape hatch no
   longer fires with actionable gaps; `fixer.txt` teaches the host-vs-code
   distinction. Tests in `tests/test_fixer.py`.
5. Browser absence = `BLOCKED_HOST_SETUP`, not silent PASS; wire it to
   block green for UI runs. *(Failure 3.1, PRP Phase 5.2)* — **done**:
   `run_browser_verify` returns a `blocked_host_setup` sentinel instead of `[]`
   for missing Playwright/Chromium/start-command; `partition_product_gaps` now
   returns `(actionable, host_env, blocked)`; `_product_gates` collects blocked
   reasons; `_phases` vetoes a UI run's green when a required browser gate was
   blocked (it does NOT go to the fixer). Tests in `tests/test_host_env_gaps.py`,
   `tests/test_orchestrator.py`.
6. Add the static "served app mounts" audit rule + smoke the app route, not
   just `/`. *(Failure 3.2, 3.3)* — **done**: new `product/spa_mount_audit.py`
   statically resolves the SPA HTML entry's transitive import closure and flags
   a dead `App.{jsx,tsx}` shell and unreachable `pages/`/`views/`/`routes/`
   components (catches run-9f836de2's unmounted `App.jsx`); wired into
   `audit_served_ui` so the gaps are actionable and reach the fixer.
   `_product_gates` now also derives `runtime_smoke` paths from the contract's
   GET page-routes (via `_smoke_paths`) instead of hardcoding `/`. Tests in
   `tests/test_spa_mount_audit.py`, `tests/test_orchestrator.py`.
7. Re-run reviewer/product gates after every brief-gap fixer round.
   *(Cross-cutting 2, PRP Phase 6)* — **done**: after a reviewer-gap fix round
   returns green, `_phases` re-runs the reviewer once; a still-gapped re-review
   marks the run not green. Tests in `tests/test_orchestrator.py`.

### Milestone C — Cut the token cost ✅ DONE (8, 9, 10)

8. Stop inlining full spec + test suite into workers; digest + path
   references; bound the reminder. *(Failure 4.1, 4.2, PRP Phase 2.2)* — **done**:
   the agentic worker (`worker.run_agentic`) now gets `spec_excerpt_for_task`
   (task-relevant ACs + file_plan, not the full user-story JSON), a
   `_tests_digest_block` (test paths + covered ACs to `read_file`, not inline
   bodies), and a compact `_contract_digest` in the drift reminder instead of
   the full contract JSON. The one-shot `worker.run` keeps full bodies (it has
   no tools). Measured on a 6-test/6-AC input: the static per-turn brief drops
   ~91% (12,285 → 1,136 chars), compounding across every worker model call.
   Tests in `tests/test_worker.py`.
9. Tool-output policies (ranged reads, bounded grep/list, offload excerpts).
   *(Failure 4.3, PRP Phase 2.5)* — **done (prompt-level)**: the `read_file`
   tool description now directs the model to read only needed lines
   (`offset`+`limit`) on large files instead of a full dump that rides forward
   in context. (The hard bounds already existed: 20k-char `_clip` in the loop,
   companion offload at `tool_offload_chars`, prune in `context.py`.)
10. Proactive, measured compaction applied to reviewer/reporter loops too.
    *(Failure 4.4, PRP Phase 2.4)* — **done**: `reviewer.run_agentic` and
    `reporter.run_agentic` now forward `enable_compaction`,
    `compact_threshold_chars`, and `prune_protect_chars` from settings, so all
    four loop-driven roles share one context policy (previously they fell
    through to untuned loop defaults). Tests updated in `tests/test_reviewer.py`,
    `tests/test_reporter.py`.

### Milestone D — Configuration & robustness ✅ DONE (11, 12, 13)

11. Surface fallback routing as a run event + cost-report line. *(Failure 5)* —
    **done**: `UsageLog.summary()` now returns `fallback_calls`; the
    orchestrator emits a `_routing_note` (`MessageSent` to the client) at run
    start naming the coding roles on fallback and the lever
    (`open_weight_base_url` unset), and the reporter prompt now requires the
    cost report to state the fallback share + why. Tests in
    `tests/test_debug_log.py`, `tests/test_orchestrator.py`. (The Muse Spark
    Open day-one comparison is a manual run, not code — unchanged per README.)
12. Soft-miss reads of not-yet-existing peer files; tighten `depends_on` seam
    edges in the planner. *(Failure 6)* — **done**: `_read_file` in the
    companion executor now returns a soft "does not exist yet — owned by
    {peer}, still working" guidance (naming `list_peers`/`known_gap`) instead
    of raising `ExecutionError` when the target is inside a *different* active
    agent's lease; a missing file you own yourself is still a hard error.
    `planner.txt` `depends_on` rule now names the real seams (auth →
    chat/profile, models → routers, components → embed, app shell → entry).
    Tests in `tests/test_executor.py`.
13. Browser runtime provisioning. *(Failure 3.4, PRP Phase 5.1)* — **done**:
    new `product/browser_runtime.py` (`ensure_browser_runtime`) detects the
    Playwright package + a launchable Chromium (with versions), installs
    what's missing after approval, and verifies a headless launch; probes and
    the installer are injectable for tests. Wired as a companion tool
    (`ensure_browser_runtime` in the executor + protocol TOOLS) and called in
    `_product_gates` before `browser_verify` — so a fresh host provisions
    instead of permanently BLOCKED, and a declined/failed install leaves the
    gate BLOCKED (not silently green). The pinned versions are emitted as a
    run event (PRP 5.3). Tests in `tests/test_browser_runtime.py`,
    `tests/test_orchestrator.py`.

---

## Acceptance criteria for this plan

1. A run that reaches a green suite cannot be flipped to failed by a
   docs-publish I/O error.
2. The transcript records each tool call exactly once.
3. When the reviewer files a code-fixable gap, the fixer cannot return an
   empty patch without a diagnosis that survives a `check` rejection.
4. A UI run on a host without Playwright/Chromium ends `BLOCKED_HOST_SETUP`,
   not green.
5. A static audit flags an SPA entry that doesn't mount the router, and
   `runtime_smoke` GETs the app-shell route(s) from the contract.
6. Worker prompt tokens per task drop measurably against the run-9f836de2
   baseline (worker-5's 942k), with the same green outcome.
7. A green result is recorded only after the final fixer write is
   re-verified (suite + product gates + brief review).
8. The run's cost report states the fallback-route share when any coding
   role is on fallback.
