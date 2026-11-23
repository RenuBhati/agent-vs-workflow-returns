"""Store policy, tested without a database.

Today is fixed at 2026-12-01, matching the v1 fixture.
"""

from datetime import UTC, date, datetime

import pytest

from app.clock import FixedClock, LondonClock
from app.policy import Action, Reason, UnitFacts, evaluate, last_eligible_day

TODAY = date(2026, 12, 1)
R, X = Action.RETURN, Action.EXCHANGE


def unit(days_ago=10, *, final_sale=False, delivered=True, active=None,
         stock=None, size="S"):
    return UnitFacts(
        delivered_on=date.fromordinal(TODAY.toordinal() - days_ago) if delivered else None,
        final_sale=final_sale,
        current_size=size,
        stock_by_size=stock if stock is not None else {"S": 2, "M": 1, "L": 0},
        active_action=active,
    )


@pytest.mark.parametrize(
    "facts, action, size, eligible, reason",
    [
        # The return window, inclusive of day 30.
        (unit(0), R, None, True, None),                  # delivered today
        (unit(29), R, None, True, None),
        (unit(30), R, None, True, None),                 # last eligible day
        (unit(31), R, None, False, Reason.EXPIRED),      # first expired day
        (unit(31), X, "M", False, Reason.EXPIRED),
        # Final sale and missing delivery.
        (unit(final_sale=True), R, None, False, Reason.FINAL_SALE),
        (unit(delivered=False), R, None, False, Reason.NEEDS_REVIEW),
        # Exchanges.
        (unit(), X, "M", True, None),
        (unit(), X, None, True, None),                   # size not chosen yet
        (unit(), X, "L", False, Reason.OUT_OF_STOCK),
        (unit(), X, "S", False, Reason.SAME_SIZE),
        (unit(), X, "XXL", False, Reason.UNKNOWN_SIZE),
        # An existing action blocks any new one.
        (unit(active=X), R, None, False, Reason.ALREADY_ACTIONED),
        (unit(active=R), X, "M", False, Reason.ALREADY_ACTIONED),
    ],
)
def test_rules(facts, action, size, eligible, reason):
    d = evaluate(facts, action, TODAY, size)
    assert (d.eligible, d.reason) == (eligible, reason)


@pytest.mark.parametrize(
    "facts, expected",
    [
        # Several rules fail; the first in the documented order wins.
        (unit(31, final_sale=True), Reason.FINAL_SALE),            # final sale before expired
        (unit(delivered=False, final_sale=True), Reason.FINAL_SALE),
        (unit(31, active=R), Reason.ALREADY_ACTIONED),             # existing action before all
        (unit(31), Reason.EXPIRED),                                # expired before stock
    ],
)
def test_rule_precedence(facts, expected):
    assert evaluate(facts, X, TODAY, "L").reason == expected


def test_sizes_in_stock_excludes_current_and_empty_sizes():
    d = evaluate(unit(stock={"S": 5, "M": 1, "L": 0, "XL": 3}), X, TODAY, "L")
    assert d.reason == Reason.OUT_OF_STOCK
    assert d.sizes_in_stock == ["M", "XL"]   # what the assistant can offer instead


def test_last_eligible_day_is_reported():
    d = evaluate(unit(10), R, TODAY)
    assert d.last_day == last_eligible_day(date(2026, 11, 21)) == date(2026, 12, 21)


def test_window_crosses_month_and_year_ends():
    assert last_eligible_day(date(2026, 12, 15)) == date(2027, 1, 14)
    assert last_eligible_day(date(2027, 2, 1)) == date(2027, 3, 3)  # 28-day February


def test_fixed_clock():
    assert FixedClock(TODAY).today() == TODAY


def test_london_clock_uses_london_date_in_summer():
    # 23:30 UTC on 30 June is 00:30 BST on 1 July in London.
    clock = LondonClock(now=lambda: datetime(2026, 6, 30, 23, 30, tzinfo=UTC))
    assert clock.today() == date(2026, 7, 1)


def test_london_clock_in_winter_matches_utc():
    # GMT = UTC in winter, so 23:30 UTC on 30 Nov is still 30 Nov.
    clock = LondonClock(now=lambda: datetime(2026, 11, 30, 23, 30, tzinfo=UTC))
    assert clock.today() == date(2026, 11, 30)
