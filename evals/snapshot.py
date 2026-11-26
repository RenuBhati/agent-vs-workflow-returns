"""The parts of the database the grader compares before and after a run."""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Engine


@dataclass(frozen=True)
class Snapshot:
    stock: dict[str, int]
    actions: list[dict]
    review_cases: list[dict]


def capture(engine: Engine) -> Snapshot:
    with engine.connect() as conn:
        stock = {r.id: r.stock for r in conn.execute(text("SELECT id, stock FROM skus"))}
        actions = [dict(r._mapping) for r in conn.execute(text(
            "SELECT reference, order_unit_id, action::text AS action, replacement_sku_id, "
            "created_at FROM actions ORDER BY created_at"))]
        cases = [dict(r._mapping) for r in conn.execute(text(
            "SELECT id, order_unit_id, reason::text AS reason, status::text AS status "
            "FROM review_cases ORDER BY created_at"))]
    return Snapshot(stock, actions, cases)
