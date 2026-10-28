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
- **Companion does:** file reads/writes inside one user-chosen directory, and
  test execution in the local Docker sandbox. It is the only thing that touches
  the user's disk.
- **The user's code never lives on our server.** Snippets enter model prompts
  exactly as with any coding agent; the workspace itself never leaves their
  machine. This neutralizes Muse Code's "local-first" advantage: we match it.

## The companion

One command, three workspace modes — the user's exact ask:

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
- Requires Python 3.12+ and Docker on the user's machine; checks both on startup
  and prints exact fixes.

## Wire protocol (deliberately minimal)

JSON over one outbound WebSocket (outbound = no firewall/NAT problems; the user
never opens a port):

| Message | Direction | Payload |
| --- | --- | --- |
| `hello` | companion → server | pair code, workspace label, client version |
| `tool_call` | server → companion | `{id, tool: read_file \| write_file \| run_tests, input}` |
| `tool_result` | companion → server | `{id, ok, output \| error}` |
| `ping` / `pong` | both | liveness; server marks the floor "companion disconnected" on loss |

Tool inputs are the existing `tools/schemas.py` models; `safe_relative_path` is
enforced **again** companion-side (never trust the wire). Three tools at launch
— no host shell tool.

## Security invariants

- **Shell commands execute only inside the Docker sandbox, never on the host.**
  The companion refuses any `ShellCommand` outside the sandbox runner, same rule
  as today's server design.
- Path ownership rules (`docs/agent-patterns.md` refinement #1) are evaluated
  companion-side per tool call: workers write only inside their owned paths.
- Pairing: the web UI shows a one-time short code per run; the companion claims
  it. Good enough for a single-user hackathon demo; real auth is post-event.
- The Meta API key stays server-side. The companion never sees it — same
  property the Docker sandbox already has.

## What this changes in the existing plan

| Was (client-console.md) | Now |
| --- | --- |
| Server-side `runs/{id}/workspace/` | User's own directory via the companion |
| `GET /runs/{id}/files` + zip download | **Dropped** — files are already home. The Handoff strip shows the workspace path the companion reported, plus tests/cost |
| Sandbox runner executes on the server | Runner executes inside the companion |
| `POST /runs {idea}` starts work immediately | `POST /runs` returns a pair code; work starts when the companion connects (floor shows "waiting for your machine…") |
| Per-run `events.jsonl` for replay/fallback demo | Unchanged, still persisted **server-side** (muse-code-comparison adoption #3) |
| File-tree panel | Stretch only: companion can stream a tree listing if time allows |

The demo flow becomes: open the page → Brief the team → copy one command into a
terminal in any folder → the floor lights up. On the demo laptop, server,
companion, and browser all run on localhost, so the only external dependency is
the Meta API itself.

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

## Open questions

| Question | Leaning | Needed by |
| --- | --- | --- |
| Companion packaging: `pipx`/`uvx` one-liner vs single-file script | `uvx --from git+https://github.com/Uknowme-h/kiln kiln-companion` until a PyPI release makes bare `uvx kiln-companion` work; from a checkout, `.venv\Scripts\kiln-companion` (after `pip install -e .`) needs no network. The command the UI shows comes from the `COMPANION_COMMAND_PREFIX` setting | Oct 29 |
| Optional read-only file tree in the UI via companion listing | Stretch, after the loop is green | Nov 2 |
| git integration (auto-branch per run) | No — leave the user's git alone; mention it as post-hackathon | — |
