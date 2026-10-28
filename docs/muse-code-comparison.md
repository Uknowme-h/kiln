# Muse Code vs Kiln — how it works, and where we beat it

Status: research done 2026-10-28. Sources: Meta's release posts and docs
([research.blog](https://research.meta.ai/blog/introducing-muse-code-and-muse-spark-1-2),
[dev.meta.ai/docs/muse-code](https://dev.meta.ai/docs/muse-code),
[workflows](https://dev.meta.ai/docs/muse-code/workflows),
[goal tracking cookbook](https://dev.meta.ai/docs/cookbook/goal-tracking),
[meta-model-cookbook](https://github.com/meta-models/meta-model-cookbook)) plus
press coverage (CNBC, Reuters, InfoWorld, The Register). This is a positioning and
design-theft document: what Muse Code does, where it is genuinely strong, where
Kiln's design is stronger, and the three things worth adopting this week.

## What Muse Code is

Meta's terminal coding agent (beta, Aug 5 2026), powered by Muse Spark 1.2 and
co-trained with its harness. One-command install, interactive TUI plus headless
`muse exec`, a TS SDK, priced $5–$200/mo or pay-as-you-go ($1.25 / $4.25 per Mtok
in/out). It targets developers working in existing repos: plan changes, write code,
validate results.

## How it works (the mechanics that matter)

| Mechanism | How it works |
| --- | --- |
| Agent loop + background agents | A simple main loop plus *persistent* async background agents that stay active all session, gather context, and choose when to report back — less redundant gathering, less steering |
| Subagent fan-out | Parent spawns write-capable children, each in its own **git worktree** so parallel writes never collide; capacity 8 agents by default (1–64); children can spawn grandchildren; cancellation is cooperative (a mid-write child finishes the write) |
| Workflows | JavaScript-authored orchestration: parallel groups, dependent stages, synthesis step; up to 1,000 child tasks, ≤16 active; a `/workflows` control room with pause/skip/restart; resume reuses the longest unchanged prefix of completed child calls |
| Event log | Append-only local JSONL of every model call, tool run, approval, edit. Replay-exact, restart-safe (`muse resume` after a crash), greppable with `jq`, and usable as deterministic golden fixtures in CI |
| Background observers | Four observer agents (memory recall, skill recall, goal tracking, verification) watch one quality axis each and *propose* advisories; a reconciler decides what reaches the main agent. Each observer adds its own token spend — the docs warn this is easy to miss. (Sources conflict on whether verification is on by default; treat as uncertain) |
| Bundled skills | `/plan` (approval-gated plan grounded in real files), `/grilling` (one decision-forcing question at a time), `/grill-with-docs`, `/taste` (anti-slop UI checklist), `/goal`. Explicit-invocation only |
| Goal tracking | DECLARE → TRACK → DRIVE → VERIFY → CLOSE: a step probe re-reminds the agent every ~10 model calls without progress; a **completion audit** gates closing the goal. Meta's own failure-mode note: name the exact oracle ("done ONLY when all six checks in test_falcon_acceptance.py pass") or the agent declares victory early |
| Safety | OS sandbox on from first run; staged approvals for compound shell commands; the guardrail file can't be rewritten by the agent (review + sandbox read-only); sandbox refuses to run when containment isn't proven live; project memory loads even in untrusted workspaces (documented injection surface) |
| Model | Muse Spark 1.2, co-trained with the harness; 1M-token context; 82.9% Terminal-Bench 2.1, 59.3% DeepSWE 1.1 |

## The honest comparison

| Dimension | Muse Code | Kiln | Verdict |
| --- | --- | --- | --- |
| Maturity, context window, co-trained harness, SDK, memory, resume | Shipping product, 1M ctx, trained-in tool use | Six days old | **Muse Code wins, uncontested** |
| Parallel isolation | Git worktrees per child | Path ownership rules | **Muse Code wins** (ours is simpler, adequate at demo scale) |
| Verification independence | The agent audits *itself*: completion audit and verification observer are the same model family grading its own work; observers only *propose*. Meta documents the failure mode (goals closed early) | Tests are written from the spec by a *separate* role **before code exists**, coders cannot edit them, and the runner is plain code in Docker — green is decided by pytest, never by a model | **Kiln wins — this is the headline** |
| Product shape | A tool for developers in existing repos | One typed sentence → tested app + README + cost report, for founders and first-time builders | **Kiln wins on the Impact criterion** |
| Observability | Auditable after the fact (JSONL, control room) for developers | Live, legible office floor anyone can read at a glance, plus the same typed event log underneath | **Kiln wins for demo and judges** |
| Cost control | Meters and bills you; observer spend is "easy to miss" | Hard token ceiling in the gateway refuses calls; per-run cost report | **Kiln wins as a design value** |
| Mid-run steering | Interactive steer/cancel, side chats, session messaging | Notes triaged by the planner at phase boundaries | **Parity** — theirs is richer, ours is more disciplined |
| Model | Muse Spark 1.2 | Muse Spark 1.3 through the Model API | **Kiln is a generation ahead** |

## The positioning for judges

Do not frame Kiln as "a better Muse Code" — it is not, and that fight is not
winnable in a hackathon. Frame it as: **"Muse Code assists a developer; Kiln
replaces the first month of a founding team — and unlike any self-grading agent,
Kiln's agents never grade their own homework."** The independent test-gate is the
one structural claim no incumbent coding agent makes, and it is exactly the theme
of the hackathon's verifiable-agents workshop.

## What we adopt this week (cheap, high value)

1. **Name the exact oracle** (from goal tracking's failure-mode fix). The
   requirements analyst's spec must phrase acceptance criteria so each names its
   checkable oracle — the test author then writes exactly those tests. Costs
   nothing, lands **today (Oct 28)** with the requirements wiring, and directly
   hardens the test-gate story.
2. **No-progress step probe** (from goal tracking's DRIVE step). If the fixer loop
   burns N model calls without the failure count changing, the planner is nudged
   to re-plan instead of letting the fixer grind. Lands with the fixer on
   **Oct 30**.
3. **Persist the event log per run** (from the JSONL/replay design). Write
   `runs/{id}/events.jsonl` alongside the workspace. Two payoffs: the golden
   prompts become replayable evidence for the eval table, and a recorded green run
   becomes the **cached fallback demo** the project plan already calls for if the
   API is down at recording time. Lands with the eval harness **Nov 1–2**.

## What we deliberately do not chase

Git-worktree isolation (path ownership is enough at our scale), crash-safe resume,
computer use, memory scopes, the SDK, and anything subscription-shaped. Muse Code
has a company behind it; Kiln has one structural idea — independent verification of
a full idea-to-app loop — and the deadline says spend everything on making that one
idea undeniable.
