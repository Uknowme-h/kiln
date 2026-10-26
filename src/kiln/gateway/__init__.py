"""Model gateway: one client, role routing, a token ceiling, and a usage log."""

from kiln.gateway.budget import BudgetGuard
from kiln.gateway.client import Completion, Gateway, OpenAITransport
from kiln.gateway.errors import BudgetExceeded, GatewayError
from kiln.gateway.routing import route_for
from kiln.gateway.usage import UsageLog

__all__ = [
    "BudgetExceeded",
    "BudgetGuard",
    "Completion",
    "Gateway",
    "GatewayError",
    "OpenAITransport",
    "UsageLog",
    "route_for",
]
