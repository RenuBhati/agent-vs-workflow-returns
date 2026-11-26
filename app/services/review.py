"""Handing a case to a person. Never changes orders, stock or actions."""

import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import text

from app.services.context import Ctx
from app.services.orders import get_unit


class ReviewReason(StrEnum):
    FINAL_SALE_DISPUTE = "final_sale_dispute"
    NO_DELIVERY_RECORD = "no_delivery_record"
    POLICY_EXCEPTION = "policy_exception"    # e.g. "the manager said it was fine"
    ACTION_CONFLICT = "action_conflict"      # wants something different from an existing action
    SHOPPER_REQUEST = "shopper_request"      # asked for a person
    ASSISTANT_STUCK = "assistant_stuck"      # turn limit or repeated failure


@dataclass(frozen=True)
class ReviewCase:
    id: uuid.UUID
    reused: bool  # True when an open case for the same issue already existed


def request_review(ctx: Ctx, reason: ReviewReason, question: str,
                   unit_id: str | None = None) -> ReviewCase:
    with ctx.engine.begin() as conn:
        if unit_id is not None:
            get_unit(conn, ctx, unit_id)  # ownership check; raises not_found
        params = {"s": ctx.shopper_id, "u": unit_id, "r": reason.value, "q": question,
                  "t": ctx.trace_id}
        new_id = conn.execute(
            text("""
                INSERT INTO review_cases (shopper_id, order_unit_id, reason, question, trace_id)
                VALUES (:s, :u, :r, :q, :t)
                ON CONFLICT (shopper_id, coalesce(order_unit_id, ''), reason)
                    WHERE status = 'open' DO NOTHING
                RETURNING id
            """),
            params,
        ).scalar()
        if new_id is not None:
            return ReviewCase(new_id, False)
        existing = conn.execute(
            text("""
                SELECT id FROM review_cases
                WHERE shopper_id = :s AND coalesce(order_unit_id, '') = coalesce(:u, '')
                  AND reason = :r AND status = 'open'
            """),
            params,
        ).scalar_one()
        return ReviewCase(existing, True)
