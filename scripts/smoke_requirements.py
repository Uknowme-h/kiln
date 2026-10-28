"""One live call: run the requirements analyst against Muse Spark for real.

Spends a small amount of API credit (one completion). Verifies the wired path —
gateway, structured output, validation — end to end. Run from the repo root:

    .venv\\Scripts\\python scripts/smoke_requirements.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiln.agents import requirements  # noqa: E402
from kiln.config import load_settings  # noqa: E402
from kiln.gateway.budget import BudgetGuard  # noqa: E402
from kiln.gateway.client import Gateway  # noqa: E402
from kiln.gateway.usage import UsageLog  # noqa: E402


def main() -> None:
    settings = load_settings()
    if not settings.model_api_key:
        raise SystemExit("MODEL_API_KEY is not set")
    usage = UsageLog()
    gateway = Gateway(settings, BudgetGuard(settings.run_token_ceiling), usage)
    spec = requirements.run(gateway, "a to-do app with user accounts and a REST API")
    print(f"title: {spec.title}")
    print(f"stack: {spec.stack}")
    print(f"stories: {len(spec.user_stories)}  criteria: {len(spec.acceptance_criteria)}")
    print(f"files: {[entry.path for entry in spec.file_plan]}")
    for criterion in spec.acceptance_criteria[:3]:
        print(f"  oracle[{criterion.id}]: {criterion.oracle[:90]}")
    print(f"assumptions: {spec.assumptions}")
    print(f"usage: {usage.summary()}")


if __name__ == "__main__":
    main()
