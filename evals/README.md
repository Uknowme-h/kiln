Golden prompts live in `prompts/`. Result files written by later runs go in `results/`, which is gitignored until a run is intentionally committed.

Runs execute on the host through the local companion (see `docs/dev-environment.md`). Docker is optional (`kiln-companion connect … --sandbox docker`).

## Testing tiers — use the cheapest one that can catch the bug

| Tier | Command | Cost | Catches |
|---|---|---|---|
| Unit | `pytest` | ~90s, 0 tokens | single components |
| **E2E simulation** | `pytest -m sim` or `python scripts/sim_run.py [scenario…]` | ~4 min (one scenario ~8s), **0 tokens** | orchestration, companion, websocket, leases, scaffold, test/fix loop |
| Live eval | `python scripts/eval_live_run.py` | 25+ min, ~1–2M tokens | model output quality, prompt effectiveness, token efficiency |

The simulation (`src/kiln/simulation.py`) runs the real server, companion,
pipeline and pytest with a scripted model and fault injection. Each scenario
replays a failure a live run once paid for (blocking call → 1006, empty
handoff, companion drop, whole-root scaffold, root-relative paths, "no
changes needed", the fix loop), and `tests/test_e2e_sim.py` also has negative
controls proving each guard fails when its fix is removed. When a live run
finds a new failure, reproduce it as a scenario before fixing it — then it is
never rediscovered at live-run prices.

## Live monitored eval

`scripts/eval_live_run.py` drives a real end-to-end run and reports per-role
prompt/completion tokens (vs the run-9f836de2 baseline), whether the scaffold
was deterministic (no agent hand-written Vite/package.json in the transcript),
official fingerprints, and the mechanical checklist:

```bash
.venv\Scripts\python scripts/eval_live_run.py --idea "…" --workspace D:\tmp\eval --timeout 2400
```

Two zero-token regression harnesses guard the companion connection:

- `scripts/ws_slow_model_check.py` — real server + detached companion +
  scripted 70s planner call. Must PASS (companion stays attached).
  `--simulate-old` reproduces the pre-fix 1006 drop (blocking model call on
  the event loop) for comparison.
- `scripts/detach_control_test.py` — the companion must survive its spawning
  parent exiting (Windows console/job-object teardown).

It connects the companion with `--auto-approve`, so shell/install approvals
auto-run and the run proceeds unattended. **`--auto-approve` never answers
`ask_user` questions and never approves the plan-approval beat** — only
shell/install-style command approvals. For prompts that ask questions or where
you want to review the plan, omit it (or answer on the floor/terminal).

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
