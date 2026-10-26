"""Hard token ceiling. A call that would pass the ceiling is refused before it is sent."""

from threading import Lock

from kiln.gateway.errors import BudgetExceeded


class BudgetGuard:
    def __init__(self, ceiling: int):
        if ceiling <= 0:
            raise ValueError("ceiling must be positive")
        self.ceiling = ceiling
        self.used = 0
        self.reserved = 0
        self._lock = Lock()

    @property
    def remaining(self) -> int:
        with self._lock:
            return self.ceiling - self.used - self.reserved

    def reserve(self, tokens: int) -> None:
        if tokens <= 0:
            raise ValueError("reservation must be positive")
        with self._lock:
            if self.used + self.reserved + tokens > self.ceiling:
                raise BudgetExceeded(
                    f"need {tokens} tokens but {self.ceiling - self.used - self.reserved} remain"
                )
            self.reserved += tokens

    def commit(self, reserved: int, actual: int) -> None:
        """Replace a reservation with the measured token count."""
        if reserved < 0 or actual < 0:
            raise ValueError("token counts must be non-negative")
        with self._lock:
            if reserved > self.reserved:
                raise ValueError("commit exceeds the current reservation")
            self.reserved -= reserved
            self.used += actual

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "ceiling": self.ceiling,
                "used": self.used,
                "reserved": self.reserved,
                "remaining": self.ceiling - self.used - self.reserved,
            }

    def release(self, tokens: int) -> None:
        if tokens < 0:
            raise ValueError("release must be non-negative")
        with self._lock:
            if tokens > self.reserved:
                raise ValueError("release exceeds the current reservation")
            self.reserved -= tokens
