# Local companion: agents work on the user's machine, not our server

Status: decided 2026-10-28. This supersedes the server-side workspace and the
Handoff (zip/file-tree) parts of `docs/client-console.md`. The Brief, Note Dock,
office floor, and notes mailbox designs are unchanged.

## The constraint that forces this

A web page cannot write arbitrary files on the user's machine, and it cannot run
Docker or pytest there. That is a guarantee of the browser platform, not something
we can design around in the UI. So if the team must build in the user's own
folder — an existing repo, an empty one, or no repo at all — a small piece of
Kiln must run on the user's computer. This is the same reason Muse Code, Claude
Code, and OpenCode are all local installs.

The web UI stays exactly what it already is: the viewer and the steering wheel.

## Architecture

```text
Browser (web UI)                Kiln server                     User's machine
----------------                ------------                    --------------
Brief + notes + floor  <------> FastAPI + LangGraph pipeline  <== WebSocket ==>  kiln-companion
(SSE, POSTs)                    model gateway (API key here)     (one command,
                                event log, run registry           scoped to one dir)
                                                                  ├─ read/write files (validated paths)
                                                                  └─ Docker sandbox runner (tests)
```

- **Server keeps:** the pipeline, all Meta model calls through the credited
  gateway (attribution + budget guard stay centralized), run state, the event
  log, and the floor stream.
- **Companion does:** file reads/writes inside one user-chosen directory, host
  shell commands (with per-command approval in this terminal), stack probes and
  installs, web fetch/search, and test execution on the host (or the optional
  Docker sandbox). It is the only thing that touches the user's disk. See
  `docs/dev-environment.md`.
- **The user's code never lives on our server.** Snippets enter model prompts
  exactly as with any coding agent; the workspace itself never leaves their
  machine. This neutralizes Muse Code's "local-first" advantage: we match it.

## The companion

**Default path (UI-driven):** run a generic listener anywhere, paste the pair
code the floor shows, then pick the folder in the browser.

```powershell
# anywhere on the machine — no code, no --dir
kiln-companion listen
# terminal prompts: Paste the pair code from the Kiln UI
# then choose the workspace folder in the web UI
```

**Power-user / smoke shortcut** — still supported when you already know the folder:

```powershell
# existing repo: run at its root — agents write only inside owned paths
kiln-companion connect PAIR-CODE --dir .

# empty repo or any empty folder: same command
kiln-companion connect PAIR-CODE --dir .\empty-app

# no repo at all: create a fresh folder (git optional)
kiln-companion connect PAIR-CODE --new my-app
```

- Distribution: a small Python package (`pipx install kiln-companion` /
  `uvx kiln-companion`) — same stack as the server, reuses `paths.py`,
  `tools/schemas.py`, and `sandbox/runner.py` almost unchanged.
- `sandbox/runner.py` already takes a workspace path and shells to Docker; it
  moves from server to companion nearly verbatim. Docker Desktop becomes a
  user-side prerequisite (the demo laptop has it).
- Requires Python 3.12+ on the user's machine. Docker is optional
  (`--sandbox docker`); the default is host execution.

## Wire protocol (deliberately minimal)

JSON over one outbound WebSocket (outbound = no firewall/NAT problems; the user
never opens a port):

| Message | Direction | Payload |
| --- | --- | --- |
| `hello` | companion → server | pair code, optional workspace label, client version. Empty workspace → run status `paired` until the UI locks a folder |
| `tool_call` | server → companion | `{id, tool, input, agent?}` — includes control tools `browse_host` / `set_workspace` for the folder picker |
| `tool_result` | companion → server | `{id, ok, output \| error, code?}` |
| `progress` | companion → server | heartbeat / awaiting_approval / approval_resolved (carries the request's `lines` so the floor renders the same dialog) |
| `approval_answer` | server → companion | `{approved, scope, feedback}` — the web floor's reply, raced against the terminal prompt |
| `ping` / `pong` | both | liveness; server marks the floor "companion disconnected" on loss |

HTTP helpers for the picker: `GET /runs/{id}/browse?path=` and
`POST /runs/{id}/workspace` (`{path, create?}`). The pipeline autostarts only
after a workspace is locked (`ready`), not on bare `paired`.

Tool inputs are the existing `tools/schemas.py` models; `safe_relative_path` is
enforced **again** companion-side (never trust the wire). Shell, install, and
network tools ask the user — in the companion terminal **or** in the floor's
approval dialog (`POST /runs/{id}/approvals` relays the answer over this
socket); whichever answer arrives first wins. Agent tools are refused while
status is still `paired`.

## Security invariants

- **Host shell is allowed, but every command is approved by the user** — at
  the companion terminal or the web floor's dialog; both answers land in the
  companion, which remains the trust root that enforces them. Destructive
  patterns are refused outright. Low-risk commands (tests, version probes) may
  be remembered for the rest of the run only.
- Path ownership rules (`docs/agent-patterns.md` refinement #1) are evaluated
  companion-side per tool call via path leases: workers write only inside their
  owned paths.
- Pairing: the web UI shows a one-time short code per run; the user pastes it
  into `kiln-companion listen` (or passes it to `connect`). Good enough for a
  single-user hackathon demo; real auth is post-event.
- The Meta API key stays server-side. The companion never sees it — same
  property the Docker sandbox already has.

## What this changes in the existing plan

| Was (client-console.md) | Now |
| --- | --- |
| Server-side `runs/{id}/workspace/` | User's own directory via the companion |
| `GET /runs/{id}/files` + zip download | **Dropped** — files are already home. The Handoff strip shows the workspace path the companion reported, plus tests/cost |
| Sandbox runner executes on the server | Runner executes inside the companion |
| `POST /runs {idea}` starts work immediately | `POST /runs` returns a pair code + `listen` command; work starts after the companion pairs **and** a workspace is locked (floor: paste code → pick folder) |
| Per-run `events.jsonl` for replay/fallback demo | Unchanged, still persisted **server-side** (muse-code-comparison adoption #3) |
| File-tree panel | Stretch only: companion can stream a tree listing if time allows |

The demo flow becomes: open the page → start `kiln-companion listen` → Brief
the team → paste the pair code into the terminal → pick a folder in the UI →
the floor lights up. On the demo laptop, server, companion, and browser all run
on localhost, so the only external dependency is the Meta API itself.

## Schedule fit

| Day | Change |
| --- | --- |
| Wed Oct 28 | Unchanged: requirements analyst + `user_notes`; oracle-named acceptance criteria |
| Thu Oct 29 | Companion skeleton (`connect`, scoped file tools) + the WebSocket relay alongside `POST /runs` |
| Fri Oct 30 | Runner moves into the companion; fixer loop targets the relay |
| Sat Oct 31 – Sun Nov 1 | Workers write through the relay; full loop on a real local folder |
| Mon Nov 2 | Handoff strip (path + tests + cost), pairing UX polish, rehearsal |
| Tue Nov 3 | Freeze |

Net new work is about a day, and it *replaces* the zip/file-tree/Handoff work —
the schedule cost is close to zero.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| WebSocket relay debugging eats hackathon time | Three message types only; companion and server both log raw frames; demo path is all-localhost |
| User's Docker missing/misconfigured | Companion checks on startup with exact fix instructions; demo laptop pre-verified |
| Windows path quirks (dev machine is Windows) | `paths.py` already normalizes to POSIX workspace-relative paths; companion translates at the boundary |
| Companion disconnects mid-run | Run pauses (not fails), floor shows the disconnected state, reconnect resumes the relay |
| Workspace pointed at the kiln repo itself (seen in the wild) | CLI refuses a checkout-shaped workspace (`src/kiln` + pyproject); `--force` overrides |
| `uvicorn --reload` restarts mid-run (seen in the wild: 1012 drop) | Real runs use a stable server without `--reload`; the companion reports a restart as "the run went with it" (exit 3), since in-memory runs do not survive one |

## Open questions

| Question | Leaning | Needed by |
| --- | --- | --- |
| Companion packaging: `pipx`/`uvx` one-liner vs single-file script | `uvx --from git+https://github.com/Uknowme-h/kiln kiln-companion` until a PyPI release makes bare `uvx kiln-companion` work; from a checkout, `.venv\Scripts\kiln-companion` (after `pip install -e .`) needs no network. The command the UI shows comes from the `COMPANION_COMMAND_PREFIX` setting | Oct 29 |
| Optional read-only file tree in the UI via companion listing | Stretch, after the loop is green | Nov 2 |
| git integration (auto-branch per run) | No — leave the user's git alone; mention it as post-hackathon | — |
