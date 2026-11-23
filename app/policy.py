"""Store policy as one pure function.

No database, no clock reads: callers pass in the facts and today's date.
Services and both assistants go through evaluate(), so the rules exist once.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum

RETURN_WINDOW_DAYS = 30


class Action(StrEnum):
    RETURN = "return"
    EXCHANGE = "exchange"


class Reason(StrEnum):
    ALREADY_ACTIONED = "already_actioned"
    FINAL_SALE = "final_sale"
    NEEDS_REVIEW = "needs_review"      # no delivery record
    EXPIRED = "expired"
    SAME_SIZE = "same_size"
    UNKNOWN_SIZE = "unknown_size"
    OUT_OF_STOCK = "out_of_stock"


@dataclass(frozen=True)
class UnitFacts:
    delivered_on: date | None
    final_sale: bool
    current_size: str
    stock_by_size: dict[str, int]       # every size of the same style and colour
    active_action: Action | None = None


@dataclass(frozen=True)
class Decision:
    eligible: bool
    reason: Reason | None = None
    sizes_in_stock: list[str] = field(default_factory=list)   # other sizes, for exchanges
    last_day: date | None = None        # last eligible day, when there is a delivery date


def last_eligible_day(delivered_on: date) -> date:
    """Inclusive: delivered on 1 Nov means 1 Dec is still eligible."""
    return delivered_on + timedelta(days=RETURN_WINDOW_DAYS)


def evaluate(
    unit: UnitFacts, action: Action, today: date, requested_size: str | None = None
) -> Decision:
    """Can this unit be returned or exchanged today? First failing rule wins."""
    sizes = [
        s for s, n in unit.stock_by_size.items() if n > 0 and s != unit.current_size
    ]
    last_day = last_eligible_day(unit.delivered_on) if unit.delivered_on else None

    def no(reason: Reason) -> Decision:
        return Decision(False, reason, sizes, last_day)

    if unit.active_action is not None:
        return no(Reason.ALREADY_ACTIONED)
    if unit.final_sale:
        return no(Reason.FINAL_SALE)
    if unit.delivered_on is None:
        return no(Reason.NEEDS_REVIEW)
    if today > last_day:
        return no(Reason.EXPIRED)

    if action is Action.EXCHANGE and requested_size is not None:
        if requested_size == unit.current_size:
            return no(Reason.SAME_SIZE)
        if requested_size not in unit.stock_by_size:
            return no(Reason.UNKNOWN_SIZE)
        if unit.stock_by_size[requested_size] <= 0:
            return no(Reason.OUT_OF_STOCK)

    # An exchange with no size yet is eligible; sizes_in_stock tells the
    # assistant what to offer.
    return Decision(True, None, sizes, last_day)
