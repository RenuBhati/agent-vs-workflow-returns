"""Read-only views of the shopper's orders, and the policy check."""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.policy import Action, Decision, UnitFacts, evaluate, last_eligible_day
from app.services.context import Ctx
from app.services.errors import Refusal


@dataclass(frozen=True)
class UnitView:
    unit_id: str
    order_id: str
    style_id: str
    name: str
    colour: str
    size: str
    delivered_on: date | None
    last_day: date | None
    final_sale: bool
    active_action: str | None
    active_reference: str | None


UNIT_SQL = """
    SELECT u.id AS unit_id, o.id AS order_id, st.id AS style_id, st.name, st.colour,
           k.size, o.delivered_on, u.final_sale,
           a.action::text AS active_action, a.reference AS active_reference
    FROM order_units u
    JOIN orders o  ON o.id = u.order_id
    JOIN skus k    ON k.id = u.sku_id
    JOIN styles st ON st.id = k.style_id
    LEFT JOIN actions a ON a.order_unit_id = u.id AND a.status = 'active'
    WHERE o.shopper_id = :shopper
"""


def _view(row) -> UnitView:
    m = row._mapping
    last = last_eligible_day(m["delivered_on"]) if m["delivered_on"] else None
    return UnitView(**dict(m), last_day=last)


def list_orders(ctx: Ctx) -> list[UnitView]:
    """Every unit the session's shopper bought, newest order first."""
    with ctx.engine.connect() as conn:
        rows = conn.execute(
            text(UNIT_SQL + " ORDER BY o.placed_on DESC, u.id"), {"shopper": ctx.shopper_id}
        )
        return [_view(r) for r in rows]


def get_unit(conn: Connection, ctx: Ctx, unit_id: str) -> UnitView:
    """One unit, only if it belongs to this shopper. Not found and not yours look the same."""
    row = conn.execute(
        text(UNIT_SQL + " AND u.id = :unit"), {"shopper": ctx.shopper_id, "unit": unit_id}
    ).first()
    if row is None:
        raise Refusal("not_found", f"no unit {unit_id!r} for this shopper")
    return _view(row)


def stock_by_size(conn: Connection, style_id: str) -> dict[str, int]:
    rows = conn.execute(
        text("SELECT size, stock FROM skus WHERE style_id = :s ORDER BY id"), {"s": style_id}
    )
    return {r.size: r.stock for r in rows}


def decide(conn: Connection, ctx: Ctx, unit: UnitView, action: Action,
           size: str | None) -> Decision:
    facts = UnitFacts(
        delivered_on=unit.delivered_on,
        final_sale=unit.final_sale,
        current_size=unit.size,
        stock_by_size=stock_by_size(conn, unit.style_id),
        active_action=Action(unit.active_action) if unit.active_action else None,
    )
    return evaluate(facts, action, ctx.clock.today(), size)


def check_options(ctx: Ctx, unit_id: str, action: Action,
                  size: str | None = None) -> tuple[UnitView, Decision]:
    """Policy and stock answer for one unit. Never refuses on policy; it reports."""
    with ctx.engine.connect() as conn:
        unit = get_unit(conn, ctx, unit_id)
        return unit, decide(conn, ctx, unit, action, size)
