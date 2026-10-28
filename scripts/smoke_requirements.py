"""Live smoke: idea -> spec -> task graph -> test suite against Muse Spark.

Spends a small amount of API credit (three completions). Verifies the wired
path — gateway, structured output, validation — for every wired agent.
Run from the repo root:

    .venv\\Scripts\\python scripts/smoke_requirements.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kiln.agents import planner, requirements, test_author  # noqa: E402
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

    graph = planner.run(gateway, spec)
    print(f"\ntasks: {len(graph.tasks)} for {graph.spec_title!r}")
    for task in graph.tasks:
        print(f"  {task.id}: {task.title}  deps={task.depends_on}  owns={task.owned_paths}")

    suite = test_author.run(gateway, spec)
    print(f"\nsuite: {len(suite.files)} files for {suite.spec_title!r}")
    for file in suite.files:
        test_count = file.content.count("def test_")
        print(f"  {file.path}: {test_count} tests, covers {file.covers}")

    print(f"\nusage: {usage.summary()}")


if __name__ == "__main__":
    main()
