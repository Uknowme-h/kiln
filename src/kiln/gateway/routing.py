"""Role to model route. Coding roles use Muse Spark Open only when that endpoint is set."""

from dataclasses import dataclass

from kiln.agents.roles import OPEN_WEIGHT_ROLES, SPARK_ROLES, Role
from kiln.config import Settings


@dataclass(frozen=True)
class Route:
    role: Role
    provider: str
    model: str
    base_url: str
    api_key: str
    fallback: bool

    def public(self) -> dict[str, str | bool]:
        """Fields safe to show in the API. The key itself is never included."""
        return {
            "role": self.role.value,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "configured": bool(self.api_key and self.base_url),
            "fallback": self.fallback,
        }


def route_for(role: Role, settings: Settings) -> Route:
    if role in SPARK_ROLES:
        return Route(
            role=role,
            provider="meta",
            model=settings.spark_model,
            base_url=settings.meta_base_url,
            api_key=settings.model_api_key,
            fallback=False,
        )
    if role in OPEN_WEIGHT_ROLES and settings.open_weight_base_url:
        return Route(
            role=role,
            provider="open_weight",
            model=settings.open_weight_model,
            base_url=settings.open_weight_base_url.rstrip("/"),
            api_key=settings.open_weight_api_key,
            fallback=False,
        )
    if role in OPEN_WEIGHT_ROLES:
        return Route(
            role=role,
            provider="meta",
            model=settings.spark_model,
            base_url=settings.meta_base_url,
            api_key=settings.model_api_key,
            fallback=True,
        )
    raise ValueError(f"unknown role {role}")
