"""Pending actions — plan.md §7.2, CLAUDE.md.

The model proposes; only the user's confirmation writes. These live in memory
on purpose: a proposal is not a fact about the portfolio, it is a question
awaiting an answer, and it must not outlive the process or the 5-minute window.

Nothing in this module touches the database.
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

EXPIRY_SECONDS = 300.0  # §7.2: pending actions expire after 5 minutes


@dataclass
class PendingAction:
    id: str
    kind: Literal["add", "remove"]
    symbol: str
    quantity: float
    price: float
    # Whether the price came from the user or was filled in from the live quote.
    # The confirm card says which, so nobody confirms a number they never gave.
    price_source: Literal["user", "quote"]
    executed_at: str
    note: str | None
    created_at: float
    expires_at: float

    def to_payload(self) -> dict:
        return {
            "action_id": self.id,
            "kind": self.kind,
            "symbol": self.symbol,
            "quantity": self.quantity,
            "price": self.price,
            "price_source": self.price_source,
            "executed_at": self.executed_at,
            "note": self.note,
            "total": round(self.quantity * self.price, 2),
            "expires_at": self.expires_at,
        }


class PendingActionStore:
    def __init__(self, ttl: float = EXPIRY_SECONDS, clock=time.time) -> None:
        self._ttl = ttl
        self._clock = clock
        self._actions: dict[str, PendingAction] = {}

    def propose(
        self,
        *,
        kind: Literal["add", "remove"],
        symbol: str,
        quantity: float,
        price: float,
        price_source: Literal["user", "quote"],
        executed_at: str,
        note: str | None = None,
    ) -> PendingAction:
        self._sweep()
        now = self._clock()
        action = PendingAction(
            id=str(uuid.uuid4()),
            kind=kind,
            symbol=symbol,
            quantity=quantity,
            price=price,
            price_source=price_source,
            executed_at=executed_at,
            note=note,
            created_at=now,
            expires_at=now + self._ttl,
        )
        self._actions[action.id] = action
        return action

    def take(self, action_id: str) -> PendingAction | None:
        """Fetch and remove. Single-use: a confirmed proposal cannot be replayed
        into a second transaction by a retried request."""
        self._sweep()
        return self._actions.pop(action_id, None)

    def peek(self, action_id: str) -> PendingAction | None:
        self._sweep()
        return self._actions.get(action_id)

    def discard(self, action_id: str) -> bool:
        return self._actions.pop(action_id, None) is not None

    def _sweep(self) -> None:
        now = self._clock()
        for key in [k for k, a in self._actions.items() if now >= a.expires_at]:
            del self._actions[key]

    def __len__(self) -> int:
        self._sweep()
        return len(self._actions)


@dataclass
class RateLimiter:
    """Token bucket on chat messages — §11, so a stuck retry loop can't run up
    an API bill."""

    capacity: int = 20
    per_seconds: float = 3600.0
    clock: object = field(default=time.monotonic)

    def __post_init__(self) -> None:
        self._tokens = float(self.capacity)
        self._last = self.clock()  # type: ignore[operator]

    def allow(self) -> bool:
        now = self.clock()  # type: ignore[operator]
        self._tokens = min(
            float(self.capacity),
            self._tokens + (now - self._last) * (self.capacity / self.per_seconds),
        )
        self._last = now
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True

    def retry_after(self) -> int:
        need = max(0.0, 1.0 - self._tokens)
        return int(need * (self.per_seconds / self.capacity)) + 1
