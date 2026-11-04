# Kiln built-in skills

Short (< ~80 line) rule packs injected into agent prompts. Two kinds:

- **Product** (heuristic): `ui_product`, `prod_ops`, `security` — attached by
  `needs_*_skill` heuristics in `__init__.py`.
- **Framework** (declarative): `vite_react`, `nextjs`, `fastapi`, `express`,
  `nestjs` — auto-attached from `spec.frameworks`, role-gated.
- **Quality** (declarative): `eng_quality`, `api_contract`, `testing_quality`,
  `frontend_quality` — attached by role and product shape.

Selection is deterministic: `resolve_skills(role, frameworks, required)` builds
the set after the Spec exists; `system_prompt_for(..., extra_skills=(...))`
appends each body. Project skills (`.kiln/skills/`) are untrusted overlays —
they carry a banner and can never satisfy `task.required_skills`.

## Adding a new framework: recipe FIRST, skill SECOND

1. **Recipe** in `src/kiln/stacks/scaffold_recipes.py`.
   - Add a `Recipe` to `RECIPES`. Choose the kind honestly:
     - `argv` — there is a stable, non-interactive official create CLI. Pin the
       scaffolder version (`create-vite@6`, `create-next-app@15`,
       `@nestjs/cli@10`), pass `--yes`/`--no-interactive` flags, set
       `manifest` to the file that proves success, and list the closed
       `allowed_options`.
     - `template` — no official CLI exists (FastAPI, Flask). Write the pinned
       `template_files`. A template recipe has **no official fingerprint**; the
       eval checks it for low token spend, not fingerprints.
   - Set `manifest` to the file whose presence marks the root as scaffolded —
     this drives the dirty-root refusal.
   - Add the framework id to `FrameworkId`/`FRAMEWORK_IDS` and (if it is an API
     or UI framework) to `API_FRAMEWORKS`/`UI_FRAMEWORKS` in
     `src/kiln/schemas/spec.py`, and to `FRAMEWORK_SKILL_MAP` in
     `skills/__init__.py`.
2. **Skill** as `<framework>.txt` here. Keep it under ~80 lines: the scaffold
   command (reference only — the companion runs it), folder ownership,
   mount/router conventions, 2–4 anti-patterns, and a done-definition.
   **Distill, don't import.** Note the source + license in a footer.
3. **Tests + eval.** Register the skill in `SKILL_CATALOG`, add recipe tests
   (`tests/test_scaffold_recipes.py`) and a resolution test
   (`tests/test_definitions.py`). Bump a scaffolder version → re-run the
   scaffold eval (`tests/test_multiroot_scaffold.py`).

## License / provenance

Skill bodies are Kiln-owned text distilled from public sources. CC0 sources
(awesome-cursorrules) need no attribution; MIT (awesome-copilot) and CC-BY
(microsoft/api-guidelines, web.dev) get a one-line credit in the footer. Never
lift anthropics/skills docx/pdf/pptx/xlsx (source-available, not OSS).
