# Kiln — Product-ready scaffolding and skills (revised plan)

Status: supersedes the v1 draft. This revision fixes the multi-root ownership
model, splits recipes into argv vs template, resolves the `nextjs` stack_id
conflict, and replaces hand-written skills with packs distilled from verified
community sources (see §6 for provenance and licenses).

Design goal, restated: stop paying tokens for agents to invent
Vite/package.json/FastAPI skeletons. Scaffolding becomes a deterministic
companion operation driven by framework skills; quality stays high via short
stack-aware rule packs attached to the right roles. This is production
architecture, not a thin trial slice.

Locked decisions (unchanged from v1):

- **Multi-root products are first-class.** A run can be a python API plus a
  Vite/Next frontend (the yes-app class), not forced into one fake folder.
- **`stack_id` stays the test/toolchain profile** (`python` | `node`) for
  probe/test/JUnit. **Frameworks are a separate Spec field** driving scaffold
  recipes and skill attachment.
- **Scaffold CLIs run in the companion** via an allowlisted `scaffold_framework`
  tool (pinned argv, non-interactive). Agents never freestyle `create-react-app`
  — CRA is forbidden; React SPA = Vite.
- **Skills use progressive disclosure:** catalog + auto-inject for required
  packs; full bodies short; deep refs only on demand. Project skills remain
  untrusted overlays.

```text
idea → Requirements → Spec { stack_id, frameworks[], roots[] }
     → probe_stack (primary) → Planner (scaffold task owns seams only)
     → companion.scaffold_framework (once per root, deterministic)
     → Scaffold worker (adapt mounts/routers/health/design.md — not re-scaffold)
     → Feature workers (with quality skills) → tests → smoke → UI gates
```

---

## 0. Multi-root ownership semantics (the schema change v1 under-specified)

This is the breaking change. Decide it first; everything else builds on it.

### 0.1 The problem

`TaskGraph.ownership_and_dependencies` rejects **any** path nesting across
tasks (via `paths_conflict`), and the "at most one scaffold task" rule is
load-bearing because today's single scaffold owns the entry point and deps
manifest. Multi-root breaks both assumptions: `backend/` + `frontend/` need
**two** scaffolds, and feature tasks live *under* a root.

### 0.2 Rules (new invariants)

1. **Each root has exactly one scaffold task**, `kind=scaffold`, and it owns
   only its **seam files** inside the root (entry point, router wiring,
   manifest, env example — e.g. `backend/app/main.py`, `backend/app/db.py`,
   `frontend/src/App.tsx`, `frontend/package.json`). It never owns the whole
   root: features own the other files inside it, and the no-nesting rule
   makes a whole-root scaffold either a validation error or — before that
   check existed — a lease that silently swallowed every feature file
   (run-7f74e69c: both scaffolds wrote all the features; the feature workers'
   leases pointed at nonexistent paths and they wrote stray trees at the
   workspace root). `TaskGraph` now rejects a whole-root scaffold over
   features with a message that tells the planner what to own instead.
2. **Every task is pinned to one root** via `Task.root: str` (default `""` =
   workspace root). `owned_paths` are workspace-relative and include the root
   prefix (`frontend/src/pages/Auth.jsx`). Root-relative paths
   (`src/pages/Auth.jsx` with `root="frontend"`) are normalized by prefixing
   the root, so leases point at real files and overlaps are detectable.
3. **The workspace root itself** (top-level `README.md`, `.env.example`,
   `docs/`, a root `package.json` workspace file if used) is owned by **no
   task**. It is written only by the orchestrator (contract/design docs) and
   the reporter (README/AGENTS.md), matching current behavior. Feature and
   scaffold tasks never own workspace-root files.
4. **Scaffold-count rule changes** from "at most one scaffold" to "at most one
   scaffold **per root**; number of scaffolds == number of declared roots".
5. **`shared_files`** in the Contract are **seams, not manifests.** For a
   companion-scaffolded root, the recipe already wrote the manifest/entry;
   `shared_files` there lists only mount points, router-registration seams,
   the health path, and stable DOM ids — never `package.json`/`main.py`, which
   the recipe owns.

### 0.3 Schema

```python
# schemas/spec.py
FrameworkId = Literal[
    "fastapi", "flask", "django",
    "express", "nestjs",
    "vite_react", "nextjs",
]

class WorkspaceRoot(BaseModel):
    path: str = Field(min_length=1)          # "backend", "frontend", or "."
    role: Literal["api", "web", "fullstack"]
    framework: FrameworkId

class Spec(BaseModel):
    # ... existing fields ...
    stack_id: StackId = "python"             # primary test toolchain (unchanged)
    frameworks: list[FrameworkId] = Field(default_factory=list)
    roots: list[WorkspaceRoot] = Field(default_factory=list)  # empty/"." = single-root
```

Validation (model_validator):

- Every `frameworks` entry maps to a known recipe (§2 table).
- If both an API framework (`fastapi|flask|django|express|nestjs`) and a SPA
  framework (`vite_react`) are present → require split roots `backend/` +
  `frontend/`, **unless** `nextjs` is the only framework (it owns full stack).
- `roots` paths are unique, non-overlapping, and each names a `framework` in
  `frameworks`.

```python
# schemas/tasks.py
class Task(BaseModel):
    # ... existing fields ...
    root: str = ""                           # "" or one of spec.roots[].path
    required_skills: list[str] = Field(default_factory=list)  # built-in names only
```

Validation: `required_skills ⊆ SKILL_CATALOG` **keys** (built-ins only — a
`project:` skill can never satisfy a requirement; see §4.4). Unknown → plan
validation error, raised in the `TaskGraph` validator next to the scaffold rule.

---

## 1. The `nextjs` stack_id conflict — resolved

v1's rule "primary stack_id covers the test suite language" collides with
`nextjs`, which owns full stack but whose oracles are node (vitest/Playwright),
even when paired with a small FastAPI service.

**Rule:** the primary `stack_id` is the language of the **acceptance-oracle
suite the test author writes.**

- `frameworks == [nextjs]` (alone, full stack) → `stack_id = "node"`. No
  conflict.
- API + `vite_react` → `stack_id = "python"` if oracles are API/contract tests
  (the common case), or `"node"` if UI-oracle-heavy.
- **`frameworks == [nextjs, fastapi]` (or any API) is constrained to
  `stack_id = "node"`.** The python root is then **smoke-only** this ship: the
  API's acceptance criteria must be expressed as node-side HTTP/contract tests
  (the Next app or a node contract suite hits the FastAPI service), because
  dual JUnit suites are out of scope (§8).

This makes the uncomfortable consequence explicit instead of hiding it: a
python API paired with Next has **no pytest gate** until dual-suite lands.
Document this in the requirements prompt so the analyst deliberately picks
node and writes contract oracles, rather than drifting into a python suite that
can't gate the API.

---

## 2. Companion: allowlisted scaffold recipes

New tool `scaffold_framework` in `companion/protocol.py` `TOOLS` +
`companion/executor.py`, backed by a new `stacks/scaffold_recipes.py`.

### 2.1 Input

```json
{ "framework": "vite_react", "root": "frontend", "options": { "typescript": true, "package_manager": "npm" } }
```

`options` is a **closed enum** per recipe, never a free shell.

### 2.2 Recipe table — two kinds

v1 treated every recipe as a CLI. Python frameworks have **no official
non-interactive create CLI**, so there are two recipe kinds with different
"refuse if dirty" logic:

```python
# stacks/scaffold_recipes.py
@dataclass(frozen=True)
class Recipe:
    id: str
    kind: Literal["argv", "template"]
    # argv recipes run an official CLI; template recipes write a pinned tree
    argv: tuple[str, ...] = ()               # for kind="argv"
    template_files: tuple[tuple[str, str], ...] = ()  # (relpath, content) for kind="template"
    manifest: str = ""                        # file that marks the root as scaffolded
    min_node: tuple[int, ...] | None = None   # host floor, checked before argv run
```

**argv recipes (official CLI, pinned):**

| framework | argv (pinned, non-interactive) | manifest |
|---|---|---|
| `vite_react` | `npm create vite@6.x <root> -- --template react-ts` | `<root>/vite.config.ts` |
| `nextjs` | `npx create-next-app@15.x <root> --ts --eslint --tailwind --app --use-npm --yes --disable-git` | `<root>/next.config.ts` |
| `express` | `npm init -y` in root + write pinned `src/index.js` (template hybrid) | `<root>/package.json` |
| `nestjs` | `npx @nestjs/cli@10.x new <root> --package-manager npm --skip-git` | `<root>/nest-cli.json` |

**template recipes (no official CLI — companion writes a pinned tree):**

| framework | template_files (companion writes) | manifest |
|---|---|---|
| `fastapi` | `<root>/app/main.py`, `<root>/app/routers/__init__.py`, `<root>/requirements.txt` stub | `<root>/app/main.py` |
| `flask` | `<root>/app/__init__.py` (app factory), `<root>/requirements.txt` stub | `<root>/app/__init__.py` |
| `django` | defer — see §2.5 | — |

**Honesty note (v1 gap #3):** template recipes get **none** of the "official
scaffold fingerprint" benefit. They are deterministic and recipe-owned, but the
file content is Kiln's pinned template, not a vendor CLI's output. The eval
(§7) must therefore assert fingerprints only for argv recipes, and assert low
token spend for template recipes.

### 2.3 Behavior

- **Refuse if dirty:** if `<root>/<manifest>` already exists, refuse unless
  `options.overwrite == true` (default false). For argv recipes, also refuse if
  `<root>` is non-empty and contains a conflicting manifest.
- **Kill interactivity mechanically:** the executor runs recipe argv via
  `run_argv` with **stdin closed** and `CI=1` in the environment, plus the
  recipe's own `--yes`/`--no-interactive` flags. This is enforced by the
  recipe *wrapper*, not per-recipe luck — a scaffold must never park the run on
  `awaiting_approval` forever because a CLI prompted.
- **Version pinning:** pin the scaffolder version in argv (`create-vite@6.x`,
  `create-next-app@15.x`, `@nestjs/cli@10.x`) the same way `INSTALLERS` pins
  tool ids. CLI flags rot between releases; the authoring note (§7) makes "bump
  recipe → re-run eval" an explicit checklist step.
- **Approval:** classify as `install`; reuse the `_install_deps` approval path
  (`action="install"`, `allow_run_scope=False`, `install_lock`, `run_argv`
  timeout). Batch all roots' argv into **one** approval prompt (all lines at
  once), matching how `_install_deps` already renders multiple commands.

### 2.4 Multi-root install_deps

`install_deps_commands` and `smoke_commands` become root-aware: the executor
threads a per-root `cwd` into `run_argv`.

- python root → `python -m venv .venv` + `pip install -r requirements.txt` at
  that root.
- node root → `npm install` at that root.
- The existing global `install_lock` serializes backend pip and frontend npm —
  keep it serialized this ship; do not "optimize" into a race.

### 2.5 Django

`django-admin startproject` is non-interactive and stable, so Django *can* be
an argv recipe. But Django's test/auth/migration surface is large and Kiln has
no Django test profile. **Defer Django** to a follow-on; ship
`fastapi/flask/express/nestjs/vite_react/nextjs` now. The schema admits
`django` so the enum doesn't churn later, but the recipe table raises
"not yet supported" until the follow-on lands.

---

## 3. Orchestrator integration

In `graph/orchestrator.py`:

1. **After Spec** (`_phases`, post `requirements.run_async`): resolve
   `spec.frameworks` → framework skill set via a new `resolve_skills()` (§4);
   store on `self`. Keep `_resolve_stack(spec.stack_id)` unchanged for host
   tools.
2. **After `_plan`, before the scaffold layer runs:** for each
   `spec.roots` entry, call `scaffold_framework` once under the scaffold
   agent's scope (pre-read/claim as needed). Deterministic, recorded once in
   the transcript (no double-log with the agent loop).
3. **Scaffold worker becomes adapt-only.** Its task description (from the
   planner, §5) says "companion already scaffolded `<root>` with `<recipe>`;
   adapt mounts/routers/health/shared stubs/design.md only — do not
   re-scaffold, do not invent a bundler."
4. **Pass expanded skill kwargs** into planner / worker / fixer / reviewer /
   test_author from the resolved set.
5. **`install_deps`** runs per-root after the scaffold layer (§2.4).
6. **Smoke:** primary suite on `stack_id`; secondary root gets
   `smoke_check` (compile / `tsc --noEmit`) only, until dual-suite lands (§8).

---

## 4. Skills — architecture and resolution

### 4.1 Layers (prompt stack, unchanged ownership)

1. Core role prompt (ownership/safety) — unchanged.
2. Auto-attached product skills (`ui_product` / `prod_ops` / `security`) — keep.
3. **Framework skills** (new) — auto-attach from `spec.frameworks` to planner +
   scaffold worker + fixer/reviewer, role-gated.
4. **Engineering quality skills** (new) — attach by role and product shape.
5. On-demand `skill` tool catalog for optional depth.

### 4.2 Don't change `system_prompt_for`'s signature — add a resolver

v1 proposed replacing the three booleans with `skills: Sequence[str]`. That
touches many call sites (`worker.run`, `fixer.run` via `asyncio.to_thread`, the
orchestrator's `_skill_kwargs()` and the fixer's local `skill_kw`) and silently
alters non-agentic paths. **Instead:**

- Keep the three booleans (`ui_product`/`prod_ops`/`security`) — heuristic,
  product-level.
- Add a new function `resolve_skills(spec, role, task) -> tuple[str, ...]` that
  returns the **declarative** set (framework + quality), role-gated.
- Extend `system_prompt_for` with an **optional** `extra_skills: tuple[str, ...]
  = ()` that appends each named skill after the boolean-gated ones.
- `_skill_kwargs()` becomes a single call to `resolve_skills()` instead of three
  flags repeated in two places.

Net: fewer diffs, same result, no behavior change to non-agentic callers that
don't pass `extra_skills`.

### 4.3 Role gating

```python
FRAMEWORK_SKILL_ROLES = frozenset({"planner", "worker", "fixer", "reviewer"})
ENG_QUALITY_ROLES     = frozenset({"worker", "fixer", "reviewer"})
# api_contract / frontend_quality attach by product shape, roles below
```

Resolution rules (deterministic, after Spec exists):

- Map `spec.frameworks` → framework skills (required).
- Always attach `eng_quality` to worker/fixer/reviewer.
- Attach `api_contract` if any API framework; `frontend_quality` if any UI
  framework. Keep existing UI/security/prod_ops heuristics (they're product
  level, orthogonal).
- Merge `task.required_skills` for the active task (validated ⊆ built-in
  catalog at plan time).
- Advertise the full catalog via the `skill` tool for optional loads.

### 4.4 Project skills can't override — the *mechanical* guarantee

"Required built-ins always win" is not enforceable by stating it in a prompt.
The enforceable parts, both already half-present:

1. `_ingest_project_skill` already prepends an untrusted banner — **keep it.**
2. **Hard rule in validation:** `task.required_skills` is validated against
   `SKILL_CATALOG` keys **only**, never the effective catalog (which merges
   `project:` entries). So a project skill named to collide can never satisfy a
   requirement. This is a code check, not a prompt hope.

Beyond that, "wins on conflict" is prompt-level and always soft — say "prepend
banner + never satisfy required," and drop any illusion of a stronger guarantee.

---

## 5. Prompt updates

- **requirements.txt + requirements.py:** the analyst must emit
  `frameworks`/`roots`, not only `stack_id`. Add the oracle-language rule from
  §1: "primary `stack_id` is the language of the acceptance-oracle suite;
  `nextjs` + any API → `stack_id=node` and write node contract oracles."
- **planner.txt:** the scaffold task owns recipe **roots + contract seams**, not
  manifests. Add: "For a companion-scaffolded root, `shared_files` lists only
  mount points / router registration / health path / DOM ids; the recipe owns
  the manifest and entry — never plan a task to rewrite them. Description must
  say 'companion already scaffolded X; adapt seams only.'"
- **worker.txt / fixer.txt:** when a framework skill is present, forbid
  re-scaffolding and forbid inventing alternate bundlers. Add to
  `testing_quality`: workers/fixers never edit `tests/`.

---

## 6. The skill packs — distilled from verified sources

**This replaces v1's hand-written skill contents.** Each pack below is distilled
from the community sources found in research (provenance + license noted).
Per your out-of-scope rule, we **distill into Kiln-owned text** — we do not
import verbatim, so licenses permit it; CC0 sources need no attribution, MIT/CC
get a one-line credit in the skill footer. Each body stays < ~80 lines.

### Framework / scaffold skills

**`vite_react`** — distilled from Vite official guide (vite.dev/guide, MIT),
react.dev "Sunsetting CRA" (Feb 2025), Vite server-options doc, and the CC0
`typescript-nodejs-react-vite` cursor rule.
- Scaffold only via companion `scaffold_framework` (runs `npm create vite`);
  **never CRA** (deprecated by the React team Feb 2025).
- `index.html` is the entry at root; `src/main.tsx` mounts; scripts stay
  `dev/build/preview`. Node 20.19+.
- Router: data router (`createBrowserRouter` + `RouterProvider`) — never
  `useState` routing.
- Proxy/CORS: `server.proxy` for `/api` with `changeOrigin`; **never
  `server.cors:true`** (DNS-rebinding, per Vite docs).
- Co-locate `*.test.tsx` next to source. `.env` blocked by `server.fs.deny`;
  `VITE_*` inlined at build — no server secrets in them.

**`nextjs`** — distilled from create-next-app CLI docs and the MIT
awesome-copilot `nextjs.instructions.md` (aligned to Next.js 16.1.1, Jan 2026).
- Scaffold via companion only. Defaults: TS + App Router + Tailwind.
- `app/` for all new code; route groups `(paren)`; private `_prefix` folders.
- Server Components default; `'use client'` only for interactivity; **never
  `next/dynamic {ssr:false}` in a Server Component** — extract a Client
  Component.
- Request APIs are async: `await cookies()/headers()`; `params`/`searchParams`
  are Promises.
- Don't `fetch('/api/...')` your own Route Handlers from Server Components —
  extract to `lib/` and call directly. `NEXT_PUBLIC_*` inlined at build.

**`fastapi`** — distilled from FastAPI "Bigger Applications" + "Run a Server
Manually" (official, MIT) and the CC0 `python-fastapi-best-practices` /
`fastapi-production-architecture` cursor rules.
- Layout: `app/` package → `main.py`, `dependencies.py`, `routers/` (one
  `APIRouter` per domain); `main.py` only wires `include_router()`.
- Set `prefix/tags/dependencies/responses` on the router, not per operation.
- Prefer **lifespan** context managers over `@app.on_event`; close shared
  httpx clients in lifespan shutdown.
- Pydantic v2 for all I/O; every endpoint declares `response_model=`;
  `HTTPException` for expected errors.
- DI via `Depends()` + factories; no DI containers.
- Dev `fastapi dev`; prod `fastapi run` / `uvicorn main:app`; **`--reload`
  never in prod**.

**`express`** — distilled from the official Express Error Handling guide (5.x)
and the CC0 `es-module-nodejs-guidelines` rule. (Note: the cursor rule literally
named "Express" turned out to be a domain app spec — not distilled.)
- One module system: ESM (`"type":"module"`) for new projects; never mix
  `require`/`import`.
- Error middleware has **exactly four** args `(err,req,res,next)`, registered
  **last**.
- Express 5: async handlers auto-forward rejections; callback code passes
  `next(err)`; never leave a rejected promise unhandled.
- Delegate to default handler when `res.headersSent`; `NODE_ENV=production` so
  stack traces don't leak.
- Health route `GET /health` → `200 {status:"ok"}` before auth middleware
  (industry convention). Mount routers `app.use('/api', router)`;
  `express.json()` before routes.

**`nestjs`** — distilled from CC0 `typescript-nestjs-best-practices` and
`nestjs-anti-hallucination`, plus MIT awesome-copilot `nestjs.instructions.md`.
- Scaffold via companion (`@nestjs/cli`). Naming: `*.controller.ts`,
  `*.service.ts`, `*.module.ts`, `*.dto.ts`.
- Module per domain; thin controllers; logic in services; DTOs validated with
  class-validator; `core` module for global filters/guards.
- **`main.ts` must register
  `ValidationPipe({whitelist:true, forbidNonWhitelisted:true, transform:true})`**
  — LLMs constantly omit this (anti-hallucination rule).
- Return values from handlers; avoid `@Res() res.json()` (bypasses
  interceptors/filters).
- `ConfigService.getOrThrow()` for config/secrets; TypeORM `synchronize:false`;
  parameterized queries only. Global exception filter returns one envelope
  `{statusCode, timestamp, path, message}`.

### Quality skills (cross-cutting)

**`eng_quality`** — distilled from CC0 `anti-overengineering`,
`anti-sycophancy-code-discipline`, and `code-style-consistency` cursor rules +
the AGENTS.md sample.
- Only change what was asked; simplest solution; verify no unrequested files
  touched.
- **Evidence before claims:** never say "done" without running tests or by-eye
  verification; report status truthfully ("wrote but did not run"); say "I
  don't know" rather than invent.
- Verify a library/symbol exists against installed versions before calling it;
  never invent signatures; mark `// VERIFY:` when unsure.
- Match existing style: read 3–5 files first; mirror naming/formatting/error
  handling; prefer libraries already in use.
- Comments explain non-obvious *why* only. No secrets in code (env vars only).

**`api_contract`** — distilled from microsoft/api-guidelines (CC BY 4.0), the
CC0 `fastapi-production-architecture` rule, and the MIT nestjs error-envelope
filter.
- One error envelope API-wide (`{statusCode, message, path, timestamp}` or RFC
  7807); services throw domain exceptions; the HTTP edge maps them; stack
  traces never leak.
- Every endpoint declares its response schema (`response_model=`/DTOs) so
  OpenAPI is faithful.
- Version by path prefix; never break within a version; add fields, don't
  rename/remove.
- Side-effecting operations accept an idempotency key.
- Plural nouns, kebab-case paths, verbs via HTTP methods; correct status codes
  (201 create, 422 validation, 500 only for the unexpected).

**`testing_quality`** — distilled from CC0 `vitest-unit-testing` and
`playwright-e2e-testing` cursor rules + the AGENTS.md testing section.
- Arrange/Act/Assert; names state expected behavior; cover valid/invalid/edge.
- Mock before imports (`vi.mock()`); `clearAllMocks` in `beforeEach`; 3–5
  focused tests per file.
- **No flaky sleeps:** Playwright auto-waiting; never `waitForTimeout`;
  selectors are `data-testid` or semantic role/label, never brittle CSS/XPath.
- Oracles are concrete (`toEqual({...})`, `toHaveText(/Welcome/)`), never
  "doesn't throw".
- Workers/fixers **never edit `tests/`** to force green — fix code or flag the
  test as characterizing existing behavior.

**`frontend_quality`** — distilled from CC0 `landing-page-image-quality`, the
MIT awesome-copilot `a11y.instructions.md` (WCAG 2.2 AA, 38 anti-patterns), and
web.dev Learn Forms (CC BY 4.0). Complements `ui_product` (which owns landing/
tokens).
- No placeholder visuals/lorem: ban `placehold.co`/`picsum`/`dummyimage`;
  informative `alt`, decorative `alt=""`.
- Every input has a visible `<label>` (`htmlFor`); placeholder is never a
  label; errors via `aria-describedby` + `aria-invalid`; focus first invalid on
  failed submit.
- Native elements first (`<button>` not `<div onClick>`); icon-only buttons get
  `aria-label`; no positive `tabindex`; never remove focus outline without a
  `:focus-visible` replacement.
- No layout shift: explicit `width`/`height`/`aspect-ratio` on images.
- Done-definition: reflows at 320px; contrast 4.5:1 text / 3:1 UI; fully
  keyboard-operable; form submittable without JS.

### Provenance / license footer (one line per skill)

- CC0 (awesome-cursorrules): no attribution required; we still add a one-line
  "structure inspired by …" for honesty.
- MIT (awesome-copilot): add the MIT notice line if any phrasing is near-verbatim.
- CC BY 4.0 (microsoft/api-guidelines, web.dev): attribute if quoted.
- Avoid anthropics/skills docx/pdf/pptx/xlsx (source-available, not OSS).

---

## 7. Tests, evals, docs

**Unit:**
- Recipe table argv shape; argv vs template kinds; `scaffold_framework` refuses
  a dirty root (manifest present) unless `overwrite`.
- Skill resolution from Spec (frameworks → framework skills; role gating).
- `Task.required_skills` validated ⊆ built-in catalog (a `project:` name fails).
- `system_prompt_for` includes framework packs only for gated roles.
- Multi-root `TaskGraph`: one scaffold per root; feature `root` pinning;
  workspace-root files owned by no task.
- `nextjs` + API forces `stack_id=node` (validation).

**Commandscan:** `npm create vite@latest …`, `npx create-next-app …`, `npm
init`, `pnpm/yarn create` classified `install`.

**Prompt contract tests** (from production-readiness Phase 4): worker prompt
mentions owned paths; scaffold skill mentions no-CRA / use-companion-scaffold;
scaffold task description mentions "companion already scaffolded."

**Eval:** re-run a UI+API prompt (yes-app class). Assert:
- For **argv recipes**: workspace contains official fingerprints
  (`frontend/vite.config.*`, `frontend/package.json`) **without** a
  multi-hundred-token hand-written config in the transcript as the primary path.
- For **template recipes** (fastapi): assert the scaffold worker's transcript
  token spend on file creation is low (it adapted seams, didn't invent the
  tree) — do **not** assert official fingerprints (there are none).

**Docs:**
- Update `docs/production-readiness-plan.md` Phase 4 to record
  frameworks/roots + companion scaffold as the shipped design.
- Authoring note (docs/ or skills README): to add a framework, add the **recipe
  first** (argv or template, pinned, dirty-refusal), **then the skill** (distill,
  don't import; note license); bump → re-run the eval.

---

## 8. Out of scope (explicit, unchanged)

- Dual full test suites in the same change — secondary root gets
  `smoke_check` (compile/`tsc --noEmit`) only this ship; dual JUnit is a
  follow-on.
- Importing third-party skill repos verbatim — we distill Kiln-owned text and
  pinned commands only.
- Interactive scaffold wizards or CRA — both forbidden.

---

## 9. Implementation order (revised)

0. **Multi-root ownership semantics first** (§0): `Spec.frameworks/roots`,
   `Task.root/required_skills`, the per-root scaffold rule, workspace-root
   ownership, and the `nextjs`→node validation. This is the breaking schema
   change; nothing else is stable until it lands.
1. Recipe table (argv vs template) + companion `scaffold_framework` +
   commandscan + multi-root `install_deps`.
2. Skill files (distilled per §6) + catalog + `resolve_skills()` + optional
   `extra_skills` on `system_prompt_for` + orchestrator resolution.
3. Prompt updates: requirements, planner, worker, fixer.
4. Orchestrator wiring (scaffold before worker adapt; per-root install).
5. Tests + one eval evidence-run checklist.
