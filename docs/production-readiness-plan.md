# Kiln Production-Readiness Implementation Plan

## Current assessment

Recent changes improved context handling:

- Compaction is enabled by default.
- Large tool outputs can be offloaded to `.kiln/tool-out/`.
- Older tool results can be pruned.
- Product gates include served-UI audit, runtime smoke, and Playwright verification.

The core budget issue remains:

- `Gateway.complete()` only reserves tokens when `max_tokens` is set.
- `max_output_tokens` defaults to `None`.
- `run_token_ceiling` can therefore still be exceeded substantially.
- Unknown provider usage can still be recorded as zero.
- Retry attempts are not independently accounted for.

Primary implementation surfaces:

- `src/kiln/gateway/client.py`
- `src/kiln/gateway/budget.py`
- `src/kiln/gateway/usage.py`
- `src/kiln/agents/loop.py`
- `src/kiln/agents/worker.py`
- `src/kiln/graph/orchestrator.py`
- `src/kiln/product/browser_verify.py`
- `src/kiln/stacks/profiles.py`

## Phase 1 — Correct token governance

### 1. Add a token estimator

Create a provider-neutral estimator such as `src/kiln/gateway/tokens.py`.

Responsibilities:

- Estimate tokens for system, user, assistant, and tool-call messages.
- Estimate tool schema tokens.
- Use a conservative character-based estimate initially.
- Make the estimator replaceable per model/provider.
- Return the estimate, estimator name, and confidence.

Cover Unicode, tool-call JSON, large schemas, offloaded stubs, and empty
messages.

### 2. Make budget admission mandatory

Every request must reserve:

```text
estimated_prompt_tokens + requested_output_limit
```

When `max_tokens` is unset, use a configured completion reserve rather than
allowing an unlimited call.

Add settings such as:

```text
default_completion_reserve
minimum_completion_reserve
budget_estimation_safety_factor
```

Required behavior:

- Refuse before transport when the reservation does not fit.
- Never allow `used + reserved > ceiling`.
- Release reservations on transport failure.
- Commit actual usage and release unused reservation on success.
- Preserve concurrency safety.

### 3. Add phase and agent budgets

Use nested budgets:

```text
Project budget
  └── Run budget
       ├── Requirements
       ├── Planner
       ├── Test author
       ├── Workers
       ├── Fixer
       ├── Reviewer
       └── Reporter
```

Add configurable limits for each phase. Child budgets must never exceed the
parent's remaining budget.

Test concurrent workers, cancellation, exhausted worker budgets, fixer
escalation, and provider usage greater than the reservation.

### 4. Correct unknown-usage accounting

Extend completion and usage records with:

```text
usage_known
estimated_prompt_tokens
estimated_completion_tokens
estimated_total_tokens
reservation_tokens
attempt
request_id
```

When usage is absent, charge conservatively and mark the record as estimated.
Do not report estimates as exact values.

### 5. Account for retries

Record every transport attempt, including:

```text
attempt_number
retry_reason
transport_failed
provider_status
estimated_tokens
charged_tokens
```

Define whether ambiguous provider failures consume reserved estimated usage.
Retry spend must appear in run reports.

## Phase 2 — Prompt and context architecture

### 1. Add a structured context envelope

Create a shared context builder such as
`src/kiln/agents/context_builder.py`.

Use common sections:

```text
objective
acceptance_criteria
owned_paths
contract_refs
relevant_files
test_refs
tool_policy
completion_protocol
```

Support compact JSON, debug mode, path references, and per-agent context
selection.

### 2. Stop injecting full test suites into workers

Workers should receive relevant acceptance criteria, test paths, test names or
oracle descriptions, and failing cases when fixing. They should read exact
tests through tools instead of receiving every test body on every request.

### 3. Reduce repeated serialization

Use compact JSON and task-specific acceptance criteria. Include contract
sections only when a task touches shared seams. Keep full documents on disk
and reference them by path where possible.

### 4. Improve compaction

Use estimated prompt tokens rather than raw character counts. Compact before
the request exceeds the budget and preserve:

- Objective
- Acceptance criteria
- Owned paths
- Contract invariants
- Files changed
- Open todos
- Recent tool results
- Unresolved failures

Record compaction savings. Apply the same policy to requirements, planner,
test-author, reviewer, and reporter loops.

### 5. Add tool-specific output policies

| Tool | Policy |
|---|---|
| `read_file` | Require ranges for large files |
| `grep` | Return bounded matching context |
| `shell` | Return exit code, summary, and bounded output |
| `run_tests` | Return failing cases and summary |
| `list_dir` | Cap entries and support pagination |
| `web_fetch` | Store full result externally and return an excerpt |
| `diagnostics` | Return diagnostics relevant to owned paths |

## Phase 3 — Worker spawning and tool orchestration

Workers currently run in dependency layers. Each worker claims paths, receives
dependency references, uses an agentic tool loop, submits a structured result,
and releases its scope.

### 1. Add worker lifecycle records

Track worker creation, scope claims, context preparation, model/tool calls,
result submission, scope release, failure, and cancellation.

Use this to detect leaked leases, workers that never submit, no-progress
workers, empty results, and cancellation during shell execution.

### 2. Use task-specific tool profiles

Replace one broad worker list with profiles such as:

```text
read_only
backend
frontend
database
test_integration
debugging
review
```

Only provide tools required by the task. This reduces tool-schema tokens and
improves tool-call reliability.

### 3. Reduce step caps

Suggested defaults:

```text
worker: 16
scaffold worker: 22
complex UI worker: 22
fixer: 12
explore: 6
```

Allow escalation only when progress is demonstrated and budget remains.

### 4. Add progress-based stopping

Track files written, tests executed, diagnostics, acceptance criteria,
repeated calls, no-op edits, and unchanged failures. Stop or replan when
meaningful progress does not occur for a configured number of calls.

### 5. Make explore subagents durable

Track parent and child IDs, prompt hash, tool budget, lifecycle status, result
summary, and cancellation reason. Prevent nested exploration, orphaned
children, and child calls after parent cancellation.

## Phase 4 — Production prompts and skills

### Shipped design (this section records what is now built)

Scaffolding is a deterministic companion operation, and frameworks/roots are
first-class on the Spec — see `docs/scaffolding-and-skills-plan.md` for the
full plan and `src/kiln/stacks/scaffold_recipes.py` for the recipe table.

- **Spec** (`src/kiln/schemas/spec.py`): `stack_id` stays the test/toolchain
  profile (`python` | `node`); `frameworks` (fastapi/flask/django/express/
  nestjs/vite_react/nextjs) drive scaffold recipes and skill packs; `roots`
  declares multi-root products (an API + Vite SPA split into `backend/` +
  `frontend/`; `nextjs` alone may own one root; `nextjs` + any API forces
  `stack_id = "node"`).
- **Tasks** (`src/kiln/schemas/tasks.py`): each task may pin a `root` and list
  `required_skills` (validated against the built-in catalog only — project
  skills can never satisfy a requirement). At most one scaffold task per root.
- **Companion scaffold** (`src/kiln/companion/executor.py` +
  `stacks/scaffold_recipes.py`): `scaffold_framework` runs a pinned official
  argv recipe (Vite/Next/Nest) or writes a pinned template tree
  (FastAPI/Flask/Express — no official create CLI exists). Interactivity is
  killed (`CI=1`, stdin DEVNULL, `--yes`); a dirty root is refused unless
  `overwrite` is set. `install_deps` is multi-root aware (npm in `frontend/`,
  venv+pip in `backend/`).
- **Skills**: framework packs (auto-attached from `spec.frameworks`, role-gated
  to planner/worker/fixer/reviewer) + cross-cutting quality packs
  (`eng_quality`, `api_contract`, `testing_quality`, `frontend_quality`).
  `eng_quality` always attaches to coding roles; `api_contract`/`frontend_quality`
  attach by product shape. `resolve_skills()` builds the declarative set;
  `system_prompt_for` takes an optional `extra_skills` tuple alongside the three
  heuristic product-skill booleans. Bodies are distilled (not imported) from
  verified community sources — see the provenance footer in each skill file.

### Built-in skills (current)

Product (heuristic): `ui_product`, `prod_ops`, `security`.
Framework (declarative): `vite_react`, `nextjs`, `fastapi`, `express`, `nestjs`.
Quality (declarative): `eng_quality`, `api_contract`, `testing_quality`,
`frontend_quality`.

Project skills are loaded from `.kiln/skills/` and `.kiln/agents/`. They are
untrusted overlays: they carry a banner and can never satisfy
`task.required_skills`.

### 1. Split prompt layers

Use:

1. Core system rules:
   - Untrusted input handling
   - Tool-use rules
   - Evidence before claims
   - Ownership restrictions
   - Completion protocol
   - No test modification
   - No secret disclosure
2. Role prompt.
3. Optional skill prompt.

Do not rely only on keyword detection from the original idea.

### 2. Make skill selection declarative

Add task metadata such as:

```text
required_skills: ["security", "prod_ops"]
optional_skills: ["ui_product"]
```

Validate that required skills are loaded and cannot override core safety
rules.

### 3. Add focused built-in skills

Consider:

- `testing`
- `browser_verification`
- `database`
- `observability`
- `deployment`
- `accessibility`
- `api_contract`
- `performance`

Keep skills short and load them only when relevant.

### 4. Add prompt contract tests

Test required semantic rules rather than exact prose:

- Worker prompts mention owned paths.
- Fixer prompts include failure evidence.
- Reviewer prompts require served-UI verification.
- Reporter prompts forbid unsupported claims.
- Project skills are marked untrusted.
- Tool lists match role capabilities.

## Phase 5 — Final product verification

The current sequence runs generated tests, fixer loops, served-UI audit,
runtime smoke, browser flows, reviewer, and reporter. It is directionally
correct, but browser setup can be silently skipped.

### 1. Add browser provisioning

Add a companion operation such as `ensure_browser_runtime` that:

1. Detects the Playwright package.
2. Detects Chromium.
3. Installs the pinned browser after approval.
4. Verifies a headless launch.
5. Caches the installation.
6. Reports the installed version.

### 2. Do not silently skip browser verification

Use explicit statuses:

```text
PASS
FAIL
BLOCKED_HOST_SETUP
NOT_APPLICABLE
```

For UI products, `BLOCKED_HOST_SETUP` must prevent a green production result
unless the user explicitly accepts the limitation.

### 3. Pin browser versions

Pin the Playwright version and record the Chromium version in run metadata.
Do not depend on a user's global browser installation.

### 4. Verify real user flows

Cover startup, HTTP status, console errors, network failures, navigation,
forms, authentication, persistence across reload, error states, keyboard
focus, third-party mounts, and screenshots on failure.

### 5. Harden server lifecycle

Test missing commands, occupied ports, early process exit, slow startup,
Windows command resolution, virtual environments, and cleanup after failures.

## Phase 6 — Test-gate correctness

Centralize the green invariant:

```text
all tests passed
and skipped == 0
and errors == 0
and required product gates passed
and required browser gates passed
```

After every fixer write:

1. Mark prior results stale.
2. Re-run the complete test suite.
3. Re-run runtime smoke.
4. Re-run browser verification.
5. Re-run served-UI audit.
6. Re-check acceptance criteria.

Final artifact checks must verify contract routes, entry points, dependency
manifests, environment examples, migrations, required integrations, test
immutability, lease boundaries, and unresolved acceptance gaps.

## Phase 7 — Durable production execution

The current system keeps run state in memory and loses runs after restart.

### 1. Persist run state

Persist:

- Run metadata
- Idea and notes
- Current phase
- Task graph
- Worker states
- Leases
- Test reports
- Product-gate reports
- Usage records
- Approval requests
- Event sequence numbers
- Final status

Start with SQLite behind a storage abstraction that can later support
PostgreSQL.

### 2. Add checkpoints and resume

Checkpoint after requirements, plan approval, test authoring, each worker
layer, each test/fix attempt, product gates, and final review.

On restart, recover incomplete runs, reconcile workers, release stale leases,
and resume from the last checkpoint without repeating completed model calls.

### 3. Make events replayable

Add event IDs, run IDs, parent IDs, schema versions, hashes for large payloads,
and redaction status. Keep JSONL for local diagnostics.

## Phase 8 — Security and observability

### 1. Redact transcripts

Before persistence, remove API keys, environment values, `.env` contents,
authorization headers, and likely secrets in shell output.

Support:

```text
metrics_only
metadata_only
debug_redacted
debug_full_local_only
```

### 2. Improve error handling

Replace silent catches with structured warnings for usage listener failures,
compaction failures, workspace read failures, and host-environment skips.

### 3. Add metrics

Track:

```text
model_calls
prompt_tokens
completion_tokens
estimated_tokens
actual_tokens
retry_count
budget_rejections
budget_reservations
compaction_count
compaction_tokens_saved
tool_output_bytes
offload_count
worker_steps
worker_no_progress_events
browser_pass/fail/blocked
test_pass/fail/skip/error
```

Expose only safe aggregates through `/usage`.

## Recommended implementation order

### Milestone 1 — Stop runaway spend

- Token estimator
- Mandatory budget reservations
- Unknown-usage accounting
- Retry accounting
- Budget and concurrency tests

### Milestone 2 — Reduce prompt cost

- Shared context builder
- Task-specific context
- Tool-specific output policies
- Compaction for all agent loops
- Remove full-suite injection
- Prompt-size telemetry

### Milestone 3 — Harden worker execution

- Worker lifecycle state
- Task-specific tool profiles
- Lower step caps
- Progress detection
- Durable explore records

### Milestone 4 — Make product verification real

- Playwright/Chromium provisioning
- Explicit browser status
- No silent UI verification skip
- Final validation after every fixer patch
- Server/browser lifecycle tests

### Milestone 5 — Production persistence

- SQLite run store
- Checkpoints
- Resume
- Event replay
- Stale lease recovery

### Milestone 6 — Prompt and skills quality

- Core/role/skill prompt layers
- Declarative task skill requirements
- Testing, browser, database, and observability skills
- Prompt contract tests
- Golden evaluation runs

## Acceptance criteria

1. A request cannot be sent when projected usage exceeds the remaining budget.
2. Concurrent workers cannot collectively exceed the run ceiling.
3. Unknown usage is marked estimated and charged conservatively.
4. Retry attempts appear in usage telemetry.
5. Prompt size and context savings are measurable per call.
6. Workers receive task-relevant tests and context by default.
7. Worker tools are selected by task profile.
8. No-progress workers are stopped or escalated.
9. UI runs install and verify Chromium after approval.
10. Missing browser setup is blocked, not green.
11. Final validation runs after the final code change.
12. Skips, errors, or blocked product gates prevent a green result.
13. Run state survives server restart.
14. Completed phases are not recomputed on resume.
15. Transcripts are redacted before persistence.
16. Built-in and project skills have explicit loading policies.
17. Prompt contracts and generated-app flows have automated tests.
18. Reports distinguish observed, inferred, skipped, and unresolved evidence.

