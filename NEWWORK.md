# New work log

Dated record of what was built during the Meta hackathon. This file should stay aligned with the commit history.

## 2026-10-26

- Initialized Kiln as a new Python 3.12 project. No application code was imported from another product.
- Added the package layout for the gateway, agents, orchestration graph, sandbox, tools, and event log.
- Wired FastAPI, Pydantic models for the spec, task graph, and test report, and a LangGraph pipeline whose nodes do not call models yet.
- Added the model gateway: OpenAI-compatible client for Meta Model API, role routing, retry, token log, and a hard token ceiling.
- Added the Docker sandbox contract: no network, resource caps, no secrets in the container, and a JUnit result parser.
- Meta Model API credits were already requested. The GitHub repo `Uknowme-h/kiln` is still private.
