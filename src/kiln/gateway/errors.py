"""Errors raised by the model gateway. These are not model opinions."""


class GatewayError(RuntimeError):
    """A model call cannot be completed."""


class BudgetExceeded(GatewayError):
    """The token ceiling refused the call before it was sent."""


class TransientError(GatewayError):
    """A retryable failure: timeout, connection, or rate limit."""
