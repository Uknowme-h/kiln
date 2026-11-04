# Multi-agent system fixes — plan

Source: the multi-agent review (framing, quality, tokens, parallelism,
communication, context). Token-budget enforcement is **out of scope** for this
plan by request; everything else from the review is covered below.

Each workstream lists the problem, the change, and how it is verified. Order is
the implementation order: later streams build on earlier ones (scope alignment
needs the `deferred` field; handoffs need the scheduler's result map).

---

## WS1 — Stop framing every brief as a "v1"

| Problem | Change |
|---|---|
| `requirements.py` opening turn says "Clarify anything that changes the **first version** (API vs UI, persistence, accounts, …)" — a v1 frame plus a fixed checklist. | Neutral opening: clarify only what *this* brief leaves open and research cannot settle; no topic list, no version frame. |
| `ask_user` tool description hard-codes "auth, persistence, integration, stack, and UI direction". | Neutral description: ask about real ambiguity; prefer selectable options. |
| Analyst can never see the workspace (`read_file` stripped unconditionally; no `list_dir`/`glob`). | Analyst gets read-only `list_dir`/`glob`/`grep`/`read_file`. The orchestrator takes a top-level inventory at run start; a non-empty workspace is announced as `<existing_workspace>` to the analyst and planner ("extend/change this project, do not re-specify it from scratch"). Clarify cap raised 4 → 6 rounds to make room for a look around. |
| Planner forced to "Phase 1 only, at most five features". | Build the whole spec when it fits one run; phase only when it genuinely cannot. No hard feature cap — "prefer fewer sharp tasks". |
| Deferred scope is invisible: approval shows task lines only, cut at 14. | `TaskGraph.deferred` (AC ids parked for later). Approval shows task lines **plus** the deferred list; cap raised to 40 lines. |
| "Leave out OAuth, payments, multi-region unless the idea is those" silently trims scope. | Keep the guidance against inventing scope, but scope cuts must be recorded in `assumptions` so approval sees them. |
| Discovery digest keys off the retired five fixed headings and drops the rest. | Digest = the whole decisions log, clipped. |

## WS2 — One scope for plan, tests, fixer and reviewer

| Problem | Change |
|---|---|
| Planner phases, but the suite must cover **every** AC → deferred ACs fail forever, blame falls to the scaffold, fixer builds Phase 2 inside seam files, run ends red. | `TaskGraph.deferred` is validated against the spec (when given spec context): every AC is either covered by a task or deferred, never both, never silently dropped. The test author receives only in-scope ACs and the suite is validated against that set. |
| Test author never sees the contract, scaffold-agnostic, no framework/testing skills. | Test author gets `<contract>` (routes, modules, seams) and is told to assert against those names; it receives framework packs and `testing_quality`. |
| Reviewer judges deferred work as gaps → fixer reopened for out-of-scope work. | Reviewer gets `<deferred>`: those ACs are not gaps this run. |

## WS3 — Code quality of the agents' output

| Problem | Change |
|---|---|
| Workers told "smallest change that satisfies the suite" — caps the product at test depth. | Workers: deliver the task's done-when completely; the suite is a floor, not the spec. "Minimal diff" stays for the fixer and for edits to existing code. |
| Every worker gets every framework pack (backend worker gets Vite/React rules). | Skills resolved per task root: frameworks of the task's root only; API-only roots drop the UI product/frontend packs. Single-root / root-less tasks keep the full set. |
| Compaction summarizer sees the brief + a tiny tail (history middle lost); after compaction the brief is cut to 6k chars (design/contract/tests gone). | Summarize the conversation *after* the brief, keeping the most recent part when it must be clipped; re-inject the brief intact up to a generous cap. |
| Workers never see `docs/plan.md` (seams, DOM ids, frozen mounts). | Plan memo passed as a clipped `<plan_memo>` block to workers and the fixer. |
| Contradictions: `Contract.shared_files` schema text vs planner prompt; worker prompt names blocks it doesn't get; `eng_quality` says "ask" to roles with no ask tool. | Aligned wording. |
| Iterating on an existing project silently overwrites the user's tests / README / AGENTS.md (write_files pre-reads to bypass the ledger). | In an existing workspace, files that pre-date the run are never clobbered: colliding suite files are renamed (`test_x.py` → `test_x_kiln.py`, suite object updated so blame still maps); README/AGENTS.md go to `docs/kiln-*.md`. |
| Scaffold on an existing root reports "failed". | "already scaffolded" is reported as reuse, not failure. |

## WS4 — Token hygiene (non-budget)

| Problem | Change |
|---|---|
| Pruning rewrites history every step → defeats provider prompt caching. | Prune in batches: only when the reclaimable stale bytes exceed a threshold. |
| Agentic reviewer gets a 25×4k-char inline snapshot; agentic fixer gets every blamed file inline. | Agentic modes get a path index and read what they need; one-shot modes keep inline content (they have no tools). |
| Design/discovery re-read per worker. | Read once per scheduling stage, refreshed when a task that owns them finishes. |
| New OpenAI client per call (no connection reuse). | Clients cached per (base_url, api_key). |

## WS5 — Parallelism

| Problem | Change |
|---|---|
| Layer barriers: a task waits for the whole previous layer. | Stage A: tasks with no dependencies (scaffolds) run as today → install deps → smoke → suite. Stage B: dependency-driven scheduler — each task starts when **its own** deps finish, up to `max_parallel_devs`. Smoke check per finished task; suite refresh at quiescent points (nothing running, work pending) to keep `<test_state>` meaningful. |
| One failed worker kills the run. | One retry of a task that produced nothing (fresh worker, told why the last attempt failed); a second failure stops scheduling, drains running workers, then ends honestly. Configurable (`worker_retries`). |
| Fix-attempt accounting double-counts stalls (2 passes, not 3). | Explicit: `max_fix_attempts` = fixer passes; stop early after 2 consecutive non-improving passes; escalate on the final pass or after a stall. |
| One serial fixer for everything. | Parallel fixers **only** when failing cases split cleanly by root (no case blames two roots). Cross-root failures (usually contract mismatches) keep one fixer that sees both sides — two fixers would each "fix" toward the other. |

## WS6 — Communication between agents

| Problem | Change |
|---|---|
| Dependents get only file paths; the upstream worker's summary / what_works / known_gaps never reach them. | `<dependency_handoff>` block per dependency (summary, what works, known gaps). |
| `list_peers` promises "files written so far"; it is always empty. | Executor appends each successful write/edit to the writer's peer record. |
| Fix handoff event hard-codes `from_agent="worker-1"`. | Uses the worker that actually built the blamed task. |

## WS7 — Structural

| Problem | Change |
|---|---|
| Project skills live in module globals → concurrent runs clobber each other. | Per-run `ContextVar` registry (asyncio tasks and `to_thread` inherit it). |
| `graph/pipeline.py` (LangGraph) is unused by the live pipeline. | **Not removed** — deleting a public module and its dependency is an owner decision. Flagged in the summary. |

---

## Status — implemented

All streams above are implemented, with these deliberate choices:

- **Coverage strictness** — a graph is held to complete coverage only when it
  tracks coverage at all (any `covers` or `deferred`). If the planner's retry
  still mis-tracks coverage but the graph is otherwise sound, it is accepted
  with a warning rather than failing the run (old behaviour: blame falls back
  to the scaffold).
- **Existing-project protection** applies only when the run-start inventory
  is non-empty (ignoring `.git/`, `.kiln/`, venvs, editor dirs), and only to
  files Kiln did not write this run.
- **Explore subagents** keep the worker route: with the current two routes
  (Spark / open-weight) the worker route already is the cheaper one.
- **`graph/pipeline.py`** is left in place for the owner to decide on.
- **Behaviour changes visible in existing tests**: a failing worker now gets
  one retry (`worker_retries`), and the discovery digest keeps every heading.

New settings: `worker_retries` (default 1), `parallel_fixers` (default on).

## Verification

- Unit tests per stream: requirements opening/tools; ask_user description;
  `TaskGraph.deferred` validation; suite scoped to in-scope ACs; test author
  sees the contract; reviewer told about deferred ACs; root-aware skills;
  compaction keeps the brief and the recent history; plan memo + dependency
  handoff reach workers; existing-file protection; batch pruning; client reuse;
  DAG scheduler starts a ready task while an unrelated one is still running;
  worker retry; fix-pass accounting; parallel vs single fixer split; list_peers
  files; per-run project skills isolation.
- Existing tests updated only where behaviour intentionally changed (worker
  retry, fix accounting).
- Full suite + `pytest -m sim` green.
