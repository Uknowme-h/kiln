# Kiln

Kiln turns one typed product idea into a tested web app. A requirements agent writes the spec, a planner splits the work, coding agents build in parallel, and a test runner decides when it is done. Models are Meta's: Muse Spark for planning, and Muse Spark Open for coding once that endpoint is configured.

This repository was started during the Meta hackathon on 26 October 2026. The foundation runs — the API, the model gateway, the typed contracts, and the sandbox — and the first agent is wired: the requirements analyst turns a typed idea into a validated spec with oracle-named acceptance criteria. The remaining agents are stubs.

## Stack

- Python 3.12, FastAPI, and Pydantic
- Meta Model API through the OpenAI SDK (`https://api.meta.ai/v1`, model `muse-spark-1.3`)
- LangGraph for the pipeline. Each node is a function that can be replaced. Whether library use is allowed on the Indie track is still an open question for the organizers.
- Docker for the test sandbox
- pytest and ruff

## Layout

```text
src/kiln/
  gateway/    model client, routing, budget guard, token log
  agents/     requirements, planner, test author, worker, fixer, reporter
  graph/      LangGraph pipeline and typed run state
  sandbox/    Docker runner and JUnit parser
  tools/      file and shell tool schemas
  events/     event log for the live view
  schemas/    spec, task graph, and test report
ui/           live run viewer, built after the loop is stable
docs/         design documents, starting with the client console
evals/        golden prompts and, later, results
docker/       sandbox image
tests/        unit tests for the gateway, parser, and API
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
uvicorn kiln.main:create_app --factory --reload
docker build -f docker/sandbox.Dockerfile -t kiln-sandbox:latest docker
```

`GET /health` returns the service status. `GET /routing` shows which model each role will call, without the API key. `GET /usage` shows the token counter.

Coding roles call Muse Spark until `OPEN_WEIGHT_BASE_URL` is set. The day-one tool-call comparison decides whether workers stay on Muse Spark Open.

The budget guard counts tokens, not dollars. Dollar rates still need to be copied from the Meta pricing page before a USD ceiling is enforced.

## Sandbox

Generated tests run in `kiln-sandbox` with `--network none`, a memory cap, a CPU cap, a read-only root filesystem, and a writable workspace mount. The container does not receive the API key. A report is green only when every test passed. Skipped tests are not treated as success.

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
