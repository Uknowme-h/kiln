# Contract-first collaboration between the two developers — plan

## Problem

The frontend developer builds a form against the backend's API. Today:

- the contract's `request` / `response` are free text ("User"), so exact
  fields are guessed or read out of the backend's code;
- each side hand-writes its own copy of the shapes, so they drift silently;
- `notify_peer` is one-way: the frontend cannot ask and wait for an answer;
- a contract change is just a chat message, and nothing checks it against the
  already-written tests;
- drift between contract and code shows up only at the suite or the reviewer.

## What real API-first teams do

| Practice | Why | Kiln |
|---|---|---|
| Spec first (OpenAPI / JSON Schema) | both sides agree on exact fields, types, status codes, errors before coding | **1. precise contract** |
| Generated shared types (openapi-typescript, codegen) | a renamed field becomes a type error, not a runtime bug | **2. generated types files** |
| Ask the owner (chat / PR comment) | questions answered by whoever knows | **3. `ask_peer` + answers** |
| Contract changes reviewed, versioned, announced | no silent breaking changes | **4. `propose_contract_change`** |
| Contract / consumer tests in CI | drift caught when introduced | **5. conformance check at submit** |

## Design

### 1. Precise contract (`schemas/tasks.py`)
- `FieldSpec {name, type, required, description}`.
- `ContractRoute` gains `kind` (`api` | `page`), `request_fields`,
  `response_fields`, `status` (success code), `errors` (error codes), `auth`.
  `request` / `response` stay as short summaries (back-compatible).
- `Contract` gains `error_shape` (the shared error envelope's fields).
- Validated at planning time (pydantic context `precise_contract`): an `api`
  route with a body method (POST/PUT/PATCH) must list request fields; an `api`
  route not answering 204 must list response fields; field names unique. Page
  routes are exempt. The planner's existing retry + lenient fallback apply, so
  a vague contract never kills a run.

### 2. Generated shared types (`kiln/contract.py`)
After the scaffold, the orchestrator writes from the validated contract:
- `docs/api-contract.json` — language-neutral, always;
- one typed module per root, in the root's language, at a path its toolchain
  already includes: TypeScript interfaces (`src/contract.ts` for Vite/Nest,
  `lib/contract.ts` for Next.js), Python `TypedDict`s (`app/contract.py` for
  FastAPI/Flask), JSDoc typedefs + field lists (`src/contract.js` for
  Express). Types only — importing them can never break a build.
- Owned by no task, so neither developer can edit them by hand: they change
  only through `propose_contract_change`, which regenerates them. Skipped when
  a task owns the path or the file pre-dates the run in an existing project.
- Developers are told the paths and to import them.

### 3. Ask and answer
- `ask_peer(question, wait)`: the question reaches the teammate before its
  next step with an id (`Q3`); the teammate answers with
  `notify_peer(message, reply_to="Q3")`.
- `wait=true` pauses the asker (no tokens) until the answer, the teammate's
  task ending, or `peer_answer_wait_seconds`. Refused when the teammate is
  between tasks (nobody to answer) or is itself waiting on the asker.
- Open questions and answers are shown on the board.

### 4. Contract changes through Kiln
`propose_contract_change(method, path, request_fields?, response_fields?,
status?, reason)`:
- adding a route, adding fields, or making a field optional: accepted;
- removing / renaming a field, or changing a route's status, when the suite's
  text mentions that field or route: **refused** — the suite is the judge;
- on accept: the contract is updated in place, `docs/contract.md`,
  `docs/api-contract.json` and every types module are regenerated, the
  teammate gets the exact change before its next step, and the board and run
  notes record it. Serialized by a lock.

### 5. Conformance at submit
When a developer submits, Kiln checks the files it wrote against the
contract: for every contract route implemented in those files (matched by
file-based route path or a route string literal), every request and response
field name must appear. Missing names reject the first submit with the exact
list ("implement them or propose_contract_change"); a second submit is
accepted with the drift added to known gaps — the check is a static
heuristic, so it never traps a developer.

## Edge cases

- Old plans / tests without fields: defaults keep them valid; precision is
  enforced only on fresh planner output, with a lenient fallback.
- Page routes, 204 responses, GET without body: exempt where it makes sense.
- Unknown field types: mapped to `unknown` / `Any` with the original type in
  a comment.
- Identifier collisions in generated names: deduplicated with a suffix.
- A route with path params (`/api/notes/{id}`, `:id`): params are ignored when
  matching implementation files.
- No API routes at all: no types modules, JSON still written.
- Teammate asks while I am idle: the question is on my board at my next task.
- Proposal for a route that does not exist: added as a new route.
- Simultaneous proposals: serialized; the second sees the first's result.
- A proposal racing a teammate's submit: the teammate is told immediately.

## Status — implemented

| Piece | Where |
|---|---|
| `FieldSpec`, structured routes, `error_shape`, precision check | `schemas/tasks.py`, `agents/planner.py` |
| Types generation, digest, conformance, change review | `kiln/contract.py` |
| Artifacts after the scaffold, `<contract_types>` brief, proposals, questions, submit check | `graph/orchestrator.py` |
| Q&A on the board, no mutual waits | `graph/lanes.py` |
| `ask_peer`, `notify_peer(reply_to)`, `propose_contract_change` | `tools/*`, `agents/coding_tools.py`, `agents/worker.py` |

Settings: `peer_answer_wait_seconds` (default 300).

## Verification
Unit tests for each piece plus an end-to-end form scenario (backend proposes
an added field, frontend is told, the types file changes). Full suite and
`-m sim`.
