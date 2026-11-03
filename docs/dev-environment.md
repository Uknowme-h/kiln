# Dev environment: the user's machine, with approvals

Status: decided 2026-11-01. Supersedes the "shell never on the host" and
"Kiln runs unattended; allow/deny only" lines in earlier docs.

## What changed

The coding agents no longer invent a whole file tree in one structured JSON
reply. They run a real tool loop on the **user's computer** through the local
companion: read, edit, shell, ask the user, search the web. Two developers can
work in parallel without overwriting each other because path leases are
enforced at write time.

Docker is optional. The default sandbox is `host`: tests and installs run on
the machine the companion is connected to. Pass `--sandbox docker` only when
you want the offline container.

## Approvals

Every shell command, install, and network fetch is shown **before it runs** —
both in the companion terminal and as a dialog on the web floor. The two show
the same question (the exact commands, one per line); whichever the user
answers first resolves it, and the other channel is told it was too late. The
companion remains the trust root: the floor's answer travels
browser → server → companion and is enforced there.

Replies in the terminal:

| Key | Meaning |
| --- | --- |
| `y` | allow this once |
| `a` | allow this exact low-risk command for the rest of this run (tests, version probes) |
| `n` | deny |
| `n <reason>` | deny and send the reason back to the agent as the tool error |

On the floor the same choices are buttons (allow once / allow for this run /
deny), with an optional reason field on deny. "Allow for this run" appears
only where the request is marked low-risk; a run-scope reply anywhere else is
coerced down to once, same as the terminal's `a`.

With neither an interactive terminal nor the floor's answer, everything that
needs approval is denied.

One approval is not a command: **the plan itself**. After the planner explores
the workspace and emits its task graph, the run pauses on a `review the plan
and build` prompt listing the tasks and their owned paths. `y` builds; `n
<reason>` sends the planner back once with your feedback; a second refusal ends
the run not-green. The plan is never remembered — you approve every run.

## Stacks

The requirements analyst sets `stack_id` to `python` or `node`. Between the
spec and the planner, the companion probes the machine (batched, read-only
version commands). Missing tools are offered as an install the user can allow
or deny (`winget` / `brew` / `apt-get` / …). Project dependencies install into
the workspace (`.venv` or `npm install`) with the same approval gate.

## Parallel developers

1. The planner explores read-only (list/glob/grep/read, one `ask_user`) and may
   write exactly `docs/plan.md`; its contract is rendered to `docs/contract.md`.
   Feature tasks depend on the scaffold and receive non-overlapping `owned_paths`.
2. The companion leases those paths to the agent; a write outside the lease is refused.
3. At most `max_parallel_devs` (default 2) run at once.
4. Failing tests map back to tasks through the suite's `covers` list; the fixer
   reads the contract plus the implicated files and may patch their union.

## OpenCode patterns we borrowed

Declarative permissions (`allow` / `ask` / `deny`, last match wins), OS-aware
shell selection, compound-command scanning, per-path write locks, and a
max-steps handoff. No Effect runtime, no TUI, no child-session resume — Kiln
stays plain Python + FastAPI + the companion WebSocket.
