"""Run deterministic end-to-end simulation scenarios — zero model tokens.

    .venv\\Scripts\\python scripts/sim_run.py            # every scenario
    .venv\\Scripts\\python scripts/sim_run.py happy companion_drop
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiln.simulation import SCENARIOS, run_scenario  # noqa: E402


def main() -> int:
    sys.stdout.reconfigure(errors="replace")  # Windows consoles are often cp1252
    names = sys.argv[1:] or list(SCENARIOS)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenario(s): {unknown}; known: {list(SCENARIOS)}")
    print(f"{'scenario':<22} {'result':<7} {'secs':>5} {'calls':>5} {'est.prompt':>10}  notes")
    failed = 0
    for name in names:
        workspace = Path(tempfile.mkdtemp(prefix=f"kiln-sim-{name}-")) / "ws"
        result = run_scenario(SCENARIOS[name], workspace)
        ok = not result.failures
        failed += not ok
        calls = sum(result.model_calls.values())
        tokens = sum(result.est_prompt_tokens.values())
        note = "; ".join(result.failures) if result.failures else SCENARIOS[name].why
        print(f"{name:<22} {'PASS' if ok else 'FAIL':<7} {result.seconds:>5} {calls:>5} {tokens:>10,}  {note[:110]}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
