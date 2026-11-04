# Two developers working in parallel ("lanes") — design and plan

## Goal

Replace "one fresh worker per task" with **two persistent developers** (Dev A,
Dev B) who split the planner's tasks, work in parallel, know what each other
owns and is doing, and talk to each other when a shared seam changes. Every
other stage (requirements, planner, test author, fixer, reviewer, reporter,
product gates) stays as it is.

Also in scope, because two developers in one workspace depend on them:

- skills loaded **on demand** instead of embedded in every turn;
- shell commands **verified** like OpenCode / Claude Code (classify, refuse,
  approve, protect the teammate's files);
- **self-healing**: a broken step looks for a way forward instead of ending
  the run.

## What we learned from other agents

| | OpenCode | Claude Code | Kiln today |
|---|---|---|---|
| Agents | `build`, `plan`, `general`/`explore` subagents; specialisation = prompt + permissions | subagents with own context; **agent teams** share a task list and `SendMessage` | one worker per task, fresh each time |
| Context | small harness prompt; instructions ordered most-shared → least-shared so the prefix stays cacheable | subagent gets only its prompt + env; `skills` preloaded or loaded via Skill tool | ~20k chars of prompt + skill packs every turn |
| Parallel overlap | prompt only: "avoid the same files or topics" | worktree isolation option; task list ownership | companion-enforced leases (stronger) |
| Talking | result returns to parent | `SendMessage` between teammates | none (paths only) |
| Shell | per-command allow/ask/deny patterns, parsed compound commands, external-directory checks | per-command permission rules, deny list | parsed + classified + approved; leases not checked for shell writes |
| Skills | `skill` tool, list of names + descriptions in prompt | Skill tool on demand, optional preload | full packs embedded every turn |

Kiln keeps what it already does better (leases, read-before-write, the
independent suite) and adopts: few generic agents specialised by prompt,
cache-friendly prompt order, a shared board + messages, on-demand skills.

## Design

### 1. Lanes (who does what)

- Two lanes, A and B, each a stable developer identity (`worker-1`, `worker-2`,
  so the floor keeps two desks). `max_parallel_devs = 1` gives one lane.
- **Assignment** (deterministic, `graph/lanes.py`):
  1. Split product: tasks in an **API root** go to Dev A (focus *backend*),
     tasks in a **web root** to Dev B (focus *frontend*).
  2. Everything else (single-root apps, API-only apps, fullstack/Next.js, root-less
     tasks) is placed by **list scheduling** over the dependency graph: each
     task goes to the lane where it can start earliest; ties go to the lane
     that owns its dependencies (keeps context), then to A. A chain stays with
     one developer (no fake parallelism); independent branches split.
- **Runtime**: each lane runs one task at a time, in its queue order, when the
  task's dependencies are done. An idle lane with nothing ready **steals** a
  ready task from the other lane's queue (the one that lane would reach last).
- The scaffold barrier stays: depth-0 tasks first, then install → smoke →
  suite, then the rest.

### 2. Context each developer holds

One fresh conversation per task (no ever-growing transcript), ordered for
prompt caching — identical for both developers first, task-specific last:

1. system: worker prompt + **skill index** + shell notes + team rules;
2. project brief: contract, plan memo, design, discovery, environment;
3. `<team>`: who is who, the board (every task, lane, status, files, summary),
   notes between developers;
4. `<lane_notes>`: this developer's own earlier tasks (files, decisions, gaps);
5. the task: task JSON, spec excerpt, tests to read, dependency paths +
   handoffs, test state, retry/failover note.

### 3. Communication

- `notify_peer(message)` tool: posts to the teammate. If the teammate is
  mid-task the message is delivered **before its next model call**
  (`<peer_messages>`); otherwise it appears in the board notes of its next task.
- The orchestrator posts automatic notes: "Dev B finished T3 (files …)",
  "Dev A's command changed your file X", "Dev B took T5 from your queue".
- `list_peers` (companion) now reports files each peer wrote.

### 4. Dynamic skills

- Coding agents with the `skill` tool (worker, fixer) get an **index**: name,
  one line, and *recommended* / *required* markers — not the packs. Packs load
  through `skill(name)` when the task touches that area.
- Required skills (task `required_skills`) not loaded by the first write get a
  one-time reminder in that write's result.
- Single-call roles without tools (test author, one-shot fallbacks) keep
  embedded packs — they cannot load anything.

### 5. Shell verification

Before running:
- classification as today (run / install / network / destructive / opaque);
- **new destructive git**: `git reset --hard`, `git clean -f…`, `git checkout .`
  / `-- .`, `git restore .`, `git stash` (not `list`/`show`) — they wipe the
  teammate's uncommitted work;
- **write-target preflight**: redirect targets (`>`, `>>`, `tee`, `Out-File`,
  `Set-Content`, `Add-Content`) and file commands (`rm`, `del`, `mv`, `cp`,
  `touch`, `Remove-Item`, `Move-Item`, `Copy-Item`, `New-Item`) are resolved;
  a target **outside the workspace** or **leased to the teammate** is refused
  with the owner named; unresolvable targets (variables, globs) pass to review;
- approval as today (auto-approve never skips the checks above).

After running:
- the change check skips heavy folders at any depth (`node_modules`, `.git`,
  `.venv`, `.next`, `dist`, `build`, caches) and gives up above 20k files;
- changes the teammate made **through its own tools** during the command are
  not blamed on this command;
- dependency manifests/lockfiles changed by an **install** command are allowed
  (both developers may add dependencies) and announced;
- anything else outside the lease is reported to the actor and to the owner.

### 6. Self-healing (no single failure ends the run)

| Failure | Response |
|---|---|
| Developer fails a task | fresh retry in the same lane → **failover** to the other developer (told why) → task marked failed; dependents still run, told to code against the contract and stub what they need; the fixer and reporter see it |
| Smoke check fails after a task | the same developer gets one short **repair** pass; still failing → recorded, build continues, the suite decides |
| Suite cannot run (mid-build or final) | self-heal: install the suite toolchain once and retry; still failing → mid-build state skipped / final verdict "not green", docs still written |
| Official scaffold fails or is declined | **skeleton fallback**: the root's scaffold task (or its first task) gets the recipe's essential files added to its lease and a brief to create the minimal skeleton |
| Unexpected crash | reported on the stage that failed (previous fix) |

Truly blocking errors stay blocking: no spec, no plan, plan refused, toolchain
missing with no installer.

## Edge cases checked

1. One task / one chain → Dev B idle (correct: no parallel work exists).
2. `max_parallel_devs = 1` → one lane, same flow.
3. API-only or single-root app → tasks split by dependencies, both developers
   informed via board + messages.
4. More than two roots → API-like roots to A, web-like to B, others balanced.
5. Stolen task → its owner lane is told; board shows the new owner.
6. Teammate needs a change in my file → `notify_peer` + known_gap; never a
   cross-lease write (leases still enforced).
7. Shell writes into the teammate's file → refused before running.
8. Both developers run `npm install` → serialized by the install lock;
   manifest changes allowed and announced.
9. Concurrent approval prompts → already serialized by the prompt lock.
10. Peer message while teammate idle → kept on the board for its next task.
11. Peer message pairing → injected only between turns (never between a tool
    call and its result), so the history stays valid.
12. Skeleton fallback never claims a file another task owns.
13. Failover retry IDs stay on the two desks (`worker-1` / `worker-2`).
14. A dependency failed → dependents get an explicit note instead of
    discovering missing files.
15. The fixer gets the owning developer's lane notes.

## Parallel by design, shared files by checkout (round 2)

How real teams cut total time without hurting quality, and what Kiln adopts:

| Team practice | Kiln |
|---|---|
| Vertical feature slices, not layers | planner splits each feature into its own task (model + route + component), even for API-only ideas |
| Interface-first: agree and stub contracts | scaffold writes the contract's seams as working stubs; `depends_on` only for hard dependencies |
| Tiny shared files, modular registration | entry discovers feature modules; never pre-imports a module a later task creates |
| Exclusive checkout for a shared file | `request_files`: free file → leased at once (owner told); teammate's file → queued, keep working, message on release; `wait=true` pauses (no tokens) until released; mutual waits refused |
| A teammate's upcoming work is respected | a pending task whose file is checked out waits until it is released |
| Plan review for parallelism | a plan with ≥3 tasks and no parallel step is re-split once by the planner; the faster valid plan wins; approval shows who builds what and the speed-up |
| Shared conventions, review | both developers are required to load the same framework pack + `eng_quality` (on demand); conventions and helpers are announced with `notify_peer`; reviewer/fixer/reporter unchanged |

## Status — implemented

| Area | Where |
|---|---|
| Assignment, board, messages, memory | `graph/lanes.py` |
| Scheduler, stealing, failover, repair, suite self-heal, skeleton fallback, run notes | `graph/orchestrator.py` |
| `notify_peer`, inbox delivery, team context, cache-friendly brief order | `tools/*`, `agents/loop.py`, `agents/worker.py`, `agents/coding_tools.py` |
| Skill index + required-skill reminder | `agents/skills/__init__.py`, `agents/definitions.py` |
| Shell preflight, destructive git, change attribution, fast fingerprint | `companion/commandscan.py`, `companion/executor.py` |
| Developers' notes for the fixer, run notes for the reporter | `agents/fixer.py`, `agents/reporter.py` |

Known limits, by design:

- The companion protects the files of the teammate's **current** task (its
  lease); files from the teammate's finished tasks are protected from the file
  tools (a developer may only write its own lease) but a shell command can still
  change them — the after-run check reports it only while a lease covers them.
- Write-target preflight reads literal paths only; globs and variables are left
  to the approval prompt.
- Two developers is the team size; `max_parallel_devs` above 2 still means two.

## Verification

Unit tests for assignment (split, single-root, chain, more roots, one lane),
work stealing, failover, dependency-failed notes, peer messaging (tool,
delivery, idle storage), board rendering, skill index + reminder, shell
preflight/destructive git/fingerprint/attribution, smoke repair, suite
self-heal, skeleton fallback, reporter run notes. Existing tests updated only
where behaviour intentionally changes (a smoke failure or a failed worker no
longer ends the run). Full suite + `-m sim`.
