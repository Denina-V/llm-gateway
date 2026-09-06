"""Cost accounting and per-key budgets.

The naive version -- call the provider, then add the cost to a running total --
lets N concurrent requests all pass the budget check while the balance is still
under the limit, and blow through it together. So this is reserve/settle:

  1. reserve an *upper bound* on the call's cost before dispatching,
  2. settle the difference once the real token counts come back.

Overshoot is then bounded by the estimate rather than by the concurrency.

Money is held as float here because the amounts are fractions of a cent and this
is a rate-limiter, not a general ledger. If you ever bill directly off these
numbers, move to Decimal first.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .errors import BudgetExceededError, UnknownModelError

# A rough characters-per-token ratio for English prose, used only to size the
# pre-flight reservation. It is intentionally an over-estimate: reserving too
# much briefly is safe, reserving too little defeats the point.
CHARS_PER_TOKEN = 3.5


def estimate_input_tokens(system: str | None, messages: list[dict]) -> int:
    chars = len(system or "")
    for m in messages:
        chars += len(m.get("content", "")) + 8  # +8 for role/framing overhead
    return max(1, int(chars / CHARS_PER_TOKEN))


class Pricing:
    def __init__(self, table: dict[str, tuple[float, float]]) -> None:
        self._table = dict(table)

    def known(self, model: str) -> bool:
        return model in self._table

    def models(self) -> list[str]:
        return sorted(self._table)

    def cost_usd(self, model: str, input_tokens: int, output_tokens: int) -> float:
        try:
            in_rate, out_rate = self._table[model]
        except KeyError:
            raise UnknownModelError(
                f"unknown model {model!r}",
                detail={"known_models": self.models()},
            ) from None
        return (input_tokens / 1e6) * in_rate + (output_tokens / 1e6) * out_rate

    def max_cost_usd(self, model: str, input_tokens: int, max_output_tokens: int) -> float:
        """Worst case: the model emits every token it is allowed to."""
        return self.cost_usd(model, input_tokens, max_output_tokens)


@dataclass
class KeyLedger:
    key_id: str
    limit_usd: float | None
    spent_usd: float = 0.0
    reserved_usd: float = 0.0
    requests: int = 0

    @property
    def committed_usd(self) -> float:
        return self.spent_usd + self.reserved_usd

    @property
    def remaining_usd(self) -> float | None:
        if self.limit_usd is None:
            return None
        return max(0.0, self.limit_usd - self.spent_usd)


class BudgetLedger:
    """In-process ledger.

    Single-node by design -- it is a dict behind a lock. Running more than one
    replica means the budget is enforced per replica, so back it with Redis (or
    the provider's own limits) before you scale out. Stated here rather than
    discovered later.
    """

    def __init__(self) -> None:
        self._ledgers: dict[str, KeyLedger] = {}
        self._lock = threading.Lock()

    def register(self, key_id: str, limit_usd: float | None) -> None:
        with self._lock:
            if key_id not in self._ledgers:
                self._ledgers[key_id] = KeyLedger(key_id=key_id, limit_usd=limit_usd)

    def _get(self, key_id: str) -> KeyLedger:
        ledger = self._ledgers.get(key_id)
        if ledger is None:
            ledger = KeyLedger(key_id=key_id, limit_usd=None)
            self._ledgers[key_id] = ledger
        return ledger

    def reserve(self, key_id: str, amount_usd: float) -> float:
        """Hold `amount_usd` against the key. Raises if it would breach the limit."""
        with self._lock:
            ledger = self._get(key_id)
            over = ledger.committed_usd + amount_usd > (ledger.limit_usd or 0.0)
            if ledger.limit_usd is not None and over:
                raise BudgetExceededError(
                    f"budget exhausted for key {key_id}",
                    detail={
                        "limit_usd": ledger.limit_usd,
                        "spent_usd": round(ledger.spent_usd, 6),
                        "reserved_usd": round(ledger.reserved_usd, 6),
                        "requested_usd": round(amount_usd, 6),
                    },
                )
            ledger.reserved_usd += amount_usd
            return amount_usd

    def settle(self, key_id: str, reserved: float, actual_usd: float) -> None:
        """Release the hold and book what the call actually cost."""
        with self._lock:
            ledger = self._get(key_id)
            ledger.reserved_usd = max(0.0, ledger.reserved_usd - reserved)
            ledger.spent_usd += actual_usd
            ledger.requests += 1

    def release(self, key_id: str, reserved: float) -> None:
        """The call never happened -- give the hold back, book nothing."""
        with self._lock:
            ledger = self._get(key_id)
            ledger.reserved_usd = max(0.0, ledger.reserved_usd - reserved)

    def snapshot(self, key_id: str) -> KeyLedger:
        with self._lock:
            ledger = self._get(key_id)
            return KeyLedger(
                key_id=ledger.key_id,
                limit_usd=ledger.limit_usd,
                spent_usd=ledger.spent_usd,
                reserved_usd=ledger.reserved_usd,
                requests=ledger.requests,
            )

    def all(self) -> list[KeyLedger]:
        with self._lock:
            return [self.snapshot_unlocked(k) for k in self._ledgers]

    def snapshot_unlocked(self, key_id: str) -> KeyLedger:
        ledger = self._ledgers[key_id]
        return KeyLedger(
            key_id=ledger.key_id,
            limit_usd=ledger.limit_usd,
            spent_usd=ledger.spent_usd,
            reserved_usd=ledger.reserved_usd,
            requests=ledger.requests,
        )
