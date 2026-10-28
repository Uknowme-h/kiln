"""One-shot Muse Spark capability probe.

Checks streaming, a single tool call, and a task list that must validate as a
TaskGraph. Run once from the repo root. It spends a small amount of API credit.

    .venv\\Scripts\\python scripts/probe_spark.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / "src"))

from openai import APIStatusError, OpenAI  # noqa: E402

from kiln.config import load_settings  # noqa: E402
from kiln.schemas.tasks import TaskGraph  # noqa: E402

TASK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "spec_title": {"type": "string"},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "owned_paths": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "title", "description", "depends_on", "owned_paths"],
            },
        },
    },
    "required": ["spec_title", "tasks"],
}

NOTE_TOOL = {
    "type": "function",
    "function": {
        "name": "record_note",
        "description": "Save a short note.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
        },
    },
}


def _usage(response) -> str:
    usage = getattr(response, "usage", None)
    if usage is None:
        return "usage=unknown"
    return f"prompt={usage.prompt_tokens} completion={usage.completion_tokens}"


def _fail(exc: Exception) -> str:
    if isinstance(exc, APIStatusError):
        detail = str(exc).replace("\n", " ")
        return f"fail HTTP {exc.status_code} {detail[:240]}"
    return f"fail {type(exc).__name__}: {exc}"[:240]


def probe_stream(client: OpenAI, model: str) -> str:
    try:
        stream = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with the single word: ready"}],
            max_completion_tokens=256,
            reasoning_effort="minimal",
            stream=True,
        )
        parts: list[str] = []
        chunks = 0
        for chunk in stream:
            chunks += 1
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                parts.append(delta)
        text = "".join(parts).strip()
        if not text:
            return f"stream: empty ({chunks} chunks, no visible text)"
        return f"stream: ok chunks={chunks} text={text[:40]!r}"
    except Exception as exc:
        return f"stream: {_fail(exc)}"


def probe_tool(client: OpenAI, model: str) -> str:
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": "Call the record_note tool. Set note to the single word ready.",
                }
            ],
            tools=[NOTE_TOOL],
            tool_choice="auto",
            max_completion_tokens=256,
            reasoning_effort="minimal",
        )
        message = response.choices[0].message
        calls = message.tool_calls or []
        if not calls:
            preview = (message.content or "").strip().replace("\n", " ")[:80]
            return f"tool: no call, text={preview!r} {_usage(response)}"
        call = calls[0]
        return (
            f"tool: ok name={call.function.name} args={call.function.arguments[:80]} "
            f"{_usage(response)}"
        )
    except Exception as exc:
        return f"tool: {_fail(exc)}"


def probe_json(client: OpenAI, model: str) -> str:
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Spec title: To-do app. "
                        "Task t1, title Accounts, description Sign-up and login, "
                        "owned path app/accounts.py, no dependencies. "
                        "Task t2, title Todos, description Create and list todos, "
                        "owned path app/todos.py, depends on t1."
                    ),
                }
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "task_graph", "strict": True, "schema": TASK_SCHEMA},
            },
            max_completion_tokens=800,
            reasoning_effort="low",
        )
        content = response.choices[0].message.content or ""
        graph = TaskGraph.model_validate_json(content)
        return (
            f"json: ok tasks={len(graph.tasks)} title={graph.spec_title!r} "
            f"paths={[path for task in graph.tasks for path in task.owned_paths]} "
            f"{_usage(response)}"
        )
    except Exception as exc:
        return f"json: {_fail(exc)}"


def main() -> None:
    settings = load_settings()
    if not settings.model_api_key:
        raise SystemExit("MODEL_API_KEY is not set")
    client = OpenAI(
        api_key=settings.model_api_key,
        base_url=settings.meta_base_url,
        max_retries=0,
        timeout=90,
    )
    print(f"model={settings.spark_model} base={settings.meta_base_url}")
    for probe in (probe_stream, probe_tool, probe_json):
        print(probe(client, settings.spark_model), flush=True)


if __name__ == "__main__":
    main()
