"""The only module that sends model calls.

It routes by role, sets max_tokens, retries transient failures, and records tokens.
"""

import time
from dataclasses import dataclass

from kiln.agents.roles import Role
from kiln.config import Settings
from kiln.gateway.budget import BudgetGuard
from kiln.gateway.errors import GatewayError, TransientError
from kiln.gateway.routing import Route, route_for
from kiln.gateway.usage import UsageLog


@dataclass(frozen=True)
class Completion:
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    usage_known: bool = True

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ChatTransport:
    """Replace this in tests. The OpenAI transport is the production default."""

    def complete(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        response_format: dict | None = None,
    ) -> Completion:
        raise NotImplementedError


class OpenAITransport(ChatTransport):
    def complete(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        response_format: dict | None = None,
    ) -> Completion:
        from openai import (
            APIConnectionError,
            APIStatusError,
            APITimeoutError,
            InternalServerError,
            OpenAI,
            RateLimitError,
        )

        client = OpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                **({"response_format": response_format} if response_format else {}),
            )
        except (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError) as exc:
            raise TransientError(str(exc)) from exc
        except APIStatusError as exc:
            raise GatewayError(f"Model API returned HTTP {exc.status_code}") from exc

        usage = response.usage
        content = response.choices[0].message.content or ""
        if usage is None:
            return Completion(
                content=content,
                model=response.model or model,
                prompt_tokens=0,
                completion_tokens=0,
                usage_known=False,
            )
        return Completion(
            content=content,
            model=response.model or model,
            prompt_tokens=usage.prompt_tokens or 0,
            completion_tokens=usage.completion_tokens or 0,
            usage_known=True,
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
    ):
        self.settings = settings
        self.budget = budget
        self.usage = usage
        self.transport = transport or OpenAITransport()
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds

    def complete(
        self,
        role: Role,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
        response_format: dict | None = None,
    ) -> Completion:
        self._validate_messages(messages)
        route = route_for(role, self.settings)
        if not route.api_key or not route.base_url:
            raise GatewayError(f"{route.provider} is not configured for {role.value}")
        limit = max_tokens if max_tokens is not None else self.settings.max_output_tokens
        if limit <= 0:
            raise GatewayError("max_tokens must be positive")

        self.budget.reserve(limit)
        try:
            completion = self._call_with_retry(route, messages, limit, response_format)
        except Exception:
            self.budget.release(limit)
            raise
        charged = completion.total_tokens if completion.usage_known else limit
        self.budget.commit(reserved=limit, actual=charged)
        self.usage.record(
            role=role,
            route=route,
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
        )
        return completion

    def _call_with_retry(
        self,
        route: Route,
        messages: list[dict[str, str]],
        max_tokens: int,
        response_format: dict | None = None,
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
    def _validate_messages(messages: list[dict[str, str]]) -> None:
        if not messages:
            raise GatewayError("messages must not be empty")
        allowed = {"system", "user", "assistant"}
        for message in messages:
            if set(message) != {"role", "content"} or message["role"] not in allowed:
                raise GatewayError("each message needs a role and content")
            if not isinstance(message["content"], str) or message["content"] == "":
                raise GatewayError("message content must be a non-empty string")
