# Kiln

Kiln turns one typed product idea into a tested web app. A requirements agent gathers the spec (and may ask you a few questions), a planner explores the workspace and splits the work with a shared contract — then pauses for you to approve the plan in the companion terminal — two coding agents build in parallel on your machine without overwriting each other, and a test runner decides when it is done. The contract is written to `docs/contract.md`, and clicking any agent on the floor shows its recorded model and tool I/O. Models are Meta's: Muse Spark for planning, and Muse Spark Open for coding once that endpoint is configured.

The team builds on the **user's own machine**, not the server: `POST /runs` returns a pair code, and a small local companion (`kiln-companion connect CODE --dir .`) claims it over one outbound WebSocket. File tools, shell commands, installs, and tests run locally. Every command that needs consent is approved in the companion terminal. See `docs/local-companion.md` and `docs/dev-environment.md`.

## Stack

- Python 3.12, FastAPI, and Pydantic
- Meta Model API through the OpenAI SDK (`https://api.meta.ai/v1`, model `muse-spark-1.3`)
- LangGraph for the pipeline. Each node is a function that can be replaced.
- Host execution by default (optional Docker sandbox)
- Generated apps: Python/pytest or Node/vitest
- pytest and ruff for Kiln itself

## Layout

```text
src/kiln/
  gateway/    model client, routing, budget guard, token log
  agents/     definitions, prompts, requirements (live), planner, test author, worker, fixer, reporter
  graph/      LangGraph pipeline and typed run state
  companion/  local companion: wire protocol, scoped executor, run registry, CLI
  sandbox/    Docker runner and JUnit parser
  tools/      file and shell tool schemas
  events/     event log for the live view
  schemas/    spec, task graph, test report, strict json schema
ui/           live run viewer and client console (Brief, Note Dock, Handoff)
docs/         design documents, starting with the client console
evals/        golden prompts and, later, results
docker/       sandbox image
tests/        unit tests for the gateway, parser, agents, companion, and API
```

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Create `.env` from `.env.example` if you do not already have one, and set `MODEL_API_KEY`. A `.env` that contains only the raw key is also accepted. Do not commit `.env`.

```powershell
pytest
ruff check src tests
uvicorn kiln.main:create_app --factory --reload   # dev only; real runs need a stable server (no --reload)
# optional offline sandbox:
# docker build -f docker/sandbox.Dockerfile -t kiln-sandbox:latest docker
```

For a real run: start the server **without** `--reload` (a restart drops the in-memory run), and point the companion at an empty folder or a `--new` one — never at the kiln checkout itself (the CLI refuses it unless `--force`). Approve shell/install prompts in that terminal.

`GET /health` returns the service status. `GET /routing` shows which model each role will call, without the API key. `GET /usage` shows the token counter.

Coding roles call Muse Spark until `OPEN_WEIGHT_BASE_URL` is set. The day-one tool-call comparison decides whether workers stay on Muse Spark Open.

The budget guard counts tokens, not dollars. Dollar rates still need to be copied from the Meta pricing page before a USD ceiling is enforced.

## Sandbox

By default, generated tests run on the host through the companion (`--sandbox host`). Pass `--sandbox docker` to use `kiln-sandbox` with `--network none`, memory/CPU caps, and a read-only root filesystem. The container never receives the API key. A report is green only when every test passed. Skipped tests are not treated as success.

## Evaluation

Results are filled in after the golden prompts run. Nothing here has been executed yet.

| Prompt | Tests green | Fix loops | Tokens | Cost | Wall clock |
| --- | --- | --- | --- | --- | --- |
| To-do app with accounts and a REST API | not run | | | | |
| URL shortener with click analytics | not run | | | | |
| Bookstore inventory tracker with search | not run | | | | |

## Attribution

Kiln's application code was written for this hackathon. It depends on FastAPI, Pydantic, the OpenAI Python SDK (used only as the Meta Model API client), LangGraph, Uvicorn, pytest, ruff, and the official `python:3.12-slim` image. See `NEWWORK.md` for the dated build log.

## License

MIT. See `LICENSE`.
