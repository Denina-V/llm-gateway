import pytest

from gateway.costs import BudgetLedger, Pricing, estimate_input_tokens
from gateway.errors import BudgetExceededError, UnknownModelError

PRICES = {"m": (10.0, 50.0)}  # $10/MTok in, $50/MTok out


def test_cost_is_per_million_tokens():
    p = Pricing(PRICES)
    assert p.cost_usd("m", 1_000_000, 0) == pytest.approx(10.0)
    assert p.cost_usd("m", 0, 1_000_000) == pytest.approx(50.0)
    assert p.cost_usd("m", 1_000, 2_000) == pytest.approx(0.01 + 0.10)


def test_unknown_model_names_the_known_ones():
    with pytest.raises(UnknownModelError) as excinfo:
        Pricing(PRICES).cost_usd("nope", 1, 1)
    assert excinfo.value.detail["known_models"] == ["m"]


def test_estimate_is_an_over_estimate_of_the_real_count():
    """The reservation must not under-shoot, or the budget stops being a bound."""
    messages = [{"role": "user", "content": "x" * 700}]
    assert estimate_input_tokens(None, messages) >= 200


def test_spending_accumulates_and_reports_remaining():
    ledger = BudgetLedger()
    ledger.register("k", 1.0)
    r = ledger.reserve("k", 0.4)
    ledger.settle("k", r, 0.25)
    snap = ledger.snapshot("k")
    assert snap.spent_usd == pytest.approx(0.25)
    assert snap.remaining_usd == pytest.approx(0.75)
    assert snap.requests == 1


def test_reservation_is_released_when_the_call_never_happens():
    ledger = BudgetLedger()
    ledger.register("k", 1.0)
    r = ledger.reserve("k", 0.9)
    ledger.release("k", r)
    assert ledger.snapshot("k").reserved_usd == pytest.approx(0.0)
    ledger.reserve("k", 0.9)  # the freed headroom is usable again


def test_reservations_bound_concurrent_overshoot():
    """The point of reserve/settle.

    Ten concurrent requests each *reserving* their worst case cannot all pass a
    $1 limit -- which is exactly what a post-hoc 'add the cost afterwards'
    ledger would allow, since every one of them would see a balance of $0.
    """
    ledger = BudgetLedger()
    ledger.register("k", 1.0)
    admitted = 0
    for _ in range(10):
        try:
            ledger.reserve("k", 0.3)
            admitted += 1
        except BudgetExceededError:
            pass
    assert admitted == 3, "0.3 x 3 fits under 1.0; the fourth must be refused"


def test_settling_below_the_reservation_returns_the_difference():
    ledger = BudgetLedger()
    ledger.register("k", 1.0)
    r = ledger.reserve("k", 0.8)          # worst case: all max_tokens emitted
    ledger.settle("k", r, 0.05)           # reality: a short answer
    snap = ledger.snapshot("k")
    assert snap.reserved_usd == pytest.approx(0.0)
    assert snap.remaining_usd == pytest.approx(0.95)


def test_a_key_with_no_limit_is_never_refused():
    ledger = BudgetLedger()
    ledger.register("free", None)
    for _ in range(100):
        r = ledger.reserve("free", 10.0)
        ledger.settle("free", r, 10.0)
    assert ledger.snapshot("free").remaining_usd is None
