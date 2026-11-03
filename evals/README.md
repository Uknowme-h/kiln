Golden prompts live in `prompts/`. Result files written by later runs go in `results/`, which is gitignored until a run is intentionally committed.

Runs execute on the host through the local companion (see `docs/dev-environment.md`). Docker is optional (`kiln-companion connect … --sandbox docker`).

## Mechanical checklist

After a golden run (or against any finished workspace), score product truth
without an LLM judge:

```bash
python -m evals.checklist D:\path\to\app --idea "…" --out evals/results/yes-app-checklist.json
```

Checks (gated by idea/discovery heuristics):

- `served_ui_audit` — official packages must appear in served HTML/JS; stub
  textarea hosts fail
- `design_md` / stylesheet presence when UI is in scope
- `.env.example` + `README.md` when prod_ops is in scope
- password-hash library reference when security/auth is in scope
- `AGENTS.md` (optional)

Exit code `0` only when every non-optional row passes.

## Host browser gate (UI briefs)

After suite green (and outside Docker), Kiln runs a structured Playwright
FlowPlan via companion `browser_verify` — not free-form MCP. Flows come from
`docs/browser-flows.json` (test_author) or a heuristic fallback. Host deps:

```bash
pip install playwright
playwright install chromium
```

yes-app-style briefs should fail when the drawing host is only a textarea stub
and pass when a canvas / official package mount is visible.
