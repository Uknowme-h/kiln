"""The only module that sends model calls.

It routes by role, sets max_tokens, retries transient failures, and records tokens.
"""

import asyncio
import logging
import threading
import time
from dataclasses import dataclass

from kiln.agents.roles import Role
from kiln.agents.transcript import TranscriptLog
from kiln.config import Settings
from kiln.gateway.budget import BudgetGuard
from kiln.gateway.errors import GatewayError, TransientError
from kiln.gateway.routing import Route, route_for
from kiln.gateway.usage import UsageLog

logger = logging.getLogger("kiln.gateway")


def _warn_if_on_event_loop(role: Role) -> None:
    """A model call is a blocking HTTP request that can take a minute.

    On the server's event loop it freezes everything sharing that loop —
    the companion websocket's keepalive, new handshakes, the floor stream —
    and the companion drops with 1006. Async callers must use
    ``await asyncio.to_thread(gateway.complete, ...)``. This makes a
    regression loud instead of a silent mid-run disconnect.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop in this thread: a worker thread or a sync caller
    logger.error(
        "gateway.complete(%s) called on the event loop thread — this blocks the "
        "server for the whole model call and drops the companion websocket; "
        "wrap it in asyncio.to_thread",
        role.value,
        stack_info=True,
    )


_PROVIDER_DETAIL_CHARS = 500


def _provider_detail(exc: Exception) -> str:
    """The provider's own error text, clipped — the reason a 400 happened.

    A bare "HTTP 400" left run-9bbe61f0 undiagnosable from its logs. The body
    is the provider's message about the request, never our credentials.
    """
    body = getattr(exc, "body", None)
    text = ""
    if isinstance(body, dict):
        error = body.get("error", body)
        text = error.get("message", "") if isinstance(error, dict) else str(error)
    if not text:
        text = str(getattr(exc, "message", "") or "")
    text = " ".join(text.split())
    if not text:
        return ""
    if len(text) > _PROVIDER_DETAIL_CHARS:
        text = text[:_PROVIDER_DETAIL_CHARS] + "…"
    return f": {text}"


@dataclass(frozen=True)
class ModelToolCall:
    """One function call the model asked for. `arguments` is the raw JSON text."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class Completion:
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    usage_known: bool = True
    tool_calls: tuple[ModelToolCall, ...] = ()
    finish_reason: str = "stop"

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def truncated(self) -> bool:
        """The model ran out of max_tokens before finishing (reasoning models
        spend it on hidden thinking first, so the JSON can be cut mid-stream)."""
        return self.finish_reason == "length"


class ChatTransport:
    """Replace this in tests. The OpenAI transport is the production default."""

    def complete(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict],
        max_tokens: int | None,
        response_format: dict | None = None,
        tools: list[dict] | None = None,
    ) -> Completion:
        raise NotImplementedError


class OpenAITransport(ChatTransport):
    """One OpenAI client per (base_url, api_key), reused across calls.

    A fresh client per call threw away its connection pool, paying a TLS
    handshake on every model turn. The client is thread-safe, and calls arrive
    from worker threads (asyncio.to_thread), so the cache is lock-guarded.
    """

    def __init__(self) -> None:
        self._clients: dict[tuple[str, str], object] = {}
        self._lock = threading.Lock()

    def _client(self, base_url: str, api_key: str):
        from openai import OpenAI

        key = (base_url, api_key)
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = OpenAI(api_key=api_key, base_url=base_url, max_retries=0)
                self._clients[key] = client
            return client

    def complete(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict],
        max_tokens: int | None,
        response_format: dict | None = None,
        tools: list[dict] | None = None,
    ) -> Completion:
        from openai import (
            APIConnectionError,
            APIStatusError,
            APITimeoutError,
            InternalServerError,
            RateLimitError,
        )

        client = self._client(base_url, api_key)
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                # None = omit max_tokens: the model uses its native default and
                # the system prompt guides length. A ceiling is opt-in via .env.
                **({"max_tokens": max_tokens} if max_tokens is not None else {}),
                **({"response_format": response_format} if response_format else {}),
                # tool_choice stays "auto": the only value the Model API accepts
                **({"tools": tools, "tool_choice": "auto"} if tools else {}),
            )
        except (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError) as exc:
            raise TransientError(str(exc)) from exc
        except APIStatusError as exc:
            raise GatewayError(
                f"Model API returned HTTP {exc.status_code}{_provider_detail(exc)}"
            ) from exc

        usage = response.usage
        choice = response.choices[0]
        message = choice.message
        content = message.content or ""
        finish_reason = choice.finish_reason or "stop"
        calls = tuple(
            ModelToolCall(
                id=call.id or f"call-{index}",
                name=call.function.name,
                arguments=call.function.arguments or "{}",
            )
            for index, call in enumerate(getattr(message, "tool_calls", None) or [])
        )
        if usage is None:
            return Completion(
                content=content,
                model=response.model or model,
                prompt_tokens=0,
                completion_tokens=0,
                usage_known=False,
                tool_calls=calls,
                finish_reason=finish_reason,
            )
        return Completion(
            content=content,
            model=response.model or model,
            prompt_tokens=usage.prompt_tokens or 0,
            completion_tokens=usage.completion_tokens or 0,
            usage_known=True,
            tool_calls=calls,
            finish_reason=finish_reason,
        )


class Gateway:
    def __init__(
        self,
        settings: Settings,
        budget: BudgetGuard,
        usage: UsageLog,
        transport: ChatTransport | None = None,
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
        transcript: TranscriptLog | None = None,
    ):
        self.settings = settings
        self.budget = budget
        self.usage = usage
        self.transport = transport or OpenAITransport()
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.transcript = transcript

    def complete(
        self,
        role: Role,
        messages: list[dict],
        *,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        tools: list[dict] | None = None,
        agent_id: str | None = None,
        route: Route | None = None,
    ) -> Completion:
        self._validate_messages(messages)
        _warn_if_on_event_loop(role)
        route = route or route_for(role, self.settings)
        if not route.api_key or not route.base_url:
            raise GatewayError(f"{route.provider} is not configured for {role.value}")
        limit = max_tokens if max_tokens is not None else self.settings.max_output_tokens
        if limit is not None and limit <= 0:
            raise GatewayError("max_output_tokens must be positive when set")

        # limit is None by default: no ceiling, no reservation — the model uses
        # its native default and the budget is charged the measured usage after.
        if limit is not None:
            self.budget.reserve(limit)
        try:
            completion = self._call_with_retry(route, messages, limit, response_format, tools)
        except Exception:
            if limit is not None:
                self.budget.release(limit)
            raise
        if completion.content == "" and not completion.tool_calls:
            # A reasoning model can return empty content with finish_reason="length".
            # Left alone this surfaces far downstream as a confusing JSON parse error;
            # fail here so the caller sees the real cause. With no ceiling set this is
            # rare (the native default is generous); a ceiling in .env is the lever.
            raise GatewayError(
                f"{route.model} returned no content for {role.value}"
                + (f" (max_tokens={limit})" if limit is not None else "")
                + "; the model produced an empty reply. If this recurs, set "
                "max_output_tokens in config (.env) as a ceiling — but length is "
                "normally guided by the system prompt."
            )
        charged = completion.total_tokens if completion.usage_known else (limit or 0)
        self.budget.commit(reserved=limit or 0, actual=charged)
        aid = agent_id or role.value
        self.usage.record(
            role=role,
            route=route,
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            agent_id=aid,
        )
        self._record_transcript(aid, role, messages, completion, limit)
        return completion

    def _record_transcript(
        self,
        agent_id: str,
        role: Role,
        messages: list[dict],
        completion: Completion,
        max_tokens: int | None,
    ) -> None:
        if self.transcript is None:
            return
        self.transcript.record(
            agent_id,
            role.value,
            "model_call",
            {
                "model": completion.model,
                "max_tokens": max_tokens,
                "messages": messages,
                "content": completion.content,
                "tool_calls": [
                    {"id": call.id, "name": call.name, "arguments": call.arguments}
                    for call in completion.tool_calls
                ],
                "prompt_tokens": completion.prompt_tokens,
                "completion_tokens": completion.completion_tokens,
                "total_tokens": completion.total_tokens,
                "finish_reason": completion.finish_reason,
                "truncated": completion.truncated,
            },
        )

    def _call_with_retry(
        self,
        route: Route,
        messages: list[dict],
        max_tokens: int | None,
        response_format: dict | None = None,
        tools: list[dict] | None = None,
    ) -> Completion:
        last_error: TransientError | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self.transport.complete(
                    base_url=route.base_url,
                    api_key=route.api_key,
                    model=route.model,
                    messages=messages,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    tools=tools,
                )
            except TransientError as exc:
                last_error = exc
                if attempt == self.max_attempts:
                    break
                time.sleep(self.backoff_seconds * (2 ** (attempt - 1)))
        raise GatewayError(
            f"{route.model} failed after {self.max_attempts} attempts"
        ) from last_error

    @staticmethod
    def _validate_messages(messages: list[dict]) -> None:
        if not messages:
            raise GatewayError("messages must not be empty")
        # tool results must answer a tool call of the assistant turn they follow;
        # an orphan is a history bug, and the API answers it with a bare HTTP 400
        open_calls: set[str] = set()
        for index, message in enumerate(messages):
            role = message.get("role")
            if role == "assistant":
                open_calls = {
                    call.get("id")
                    for call in message.get("tool_calls") or []
                    if isinstance(call, dict) and call.get("id")
                }
            elif role == "tool":
                call_id = message.get("tool_call_id")
                if call_id and call_id not in open_calls:
                    raise GatewayError(
                        f"message {index} is a tool result for {call_id!r}, which no "
                        "preceding assistant turn called (orphaned tool message)"
                    )
                open_calls.discard(call_id)
            else:
                open_calls = set()
        for message in messages:
            role = message.get("role")
            keys = set(message)
            content = message.get("content")
            if role in ("system", "user"):
                if keys != {"role", "content"}:
                    raise GatewayError("each message needs a role and content")
            elif role == "assistant":
                if not keys <= {"role", "content", "tool_calls"} or "content" not in keys:
                    raise GatewayError("each message needs a role and content")
                if message.get("tool_calls"):
                    # an assistant turn that only calls tools may carry no text
                    if content is not None and not isinstance(content, str):
                        raise GatewayError("message content must be a string")
                    continue
            elif role == "tool":
                if keys != {"role", "tool_call_id", "content"} or not message["tool_call_id"]:
                    raise GatewayError("a tool message needs a tool_call_id and content")
            else:
                raise GatewayError("each message needs a role and content")
            if not isinstance(content, str) or content == "":
                raise GatewayError("message content must be a non-empty string")
