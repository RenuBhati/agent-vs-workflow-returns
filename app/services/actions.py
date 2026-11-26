"""Committing a confirmed proposal, and checking what was committed.

commit_action is the one place a return or exchange is created. Everything it
does happens in a single transaction; see the numbered steps below.
"""

import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.policy import Action
from app.services.context import Ctx
from app.services.errors import Refusal
from app.services.orders import decide, get_unit

# No 0/O or 1/I, so references are easy to read out.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_reference() -> str:
    return "RX-" + "".join(secrets.choice(_ALPHABET) for _ in range(6))


@dataclass(frozen=True)
class Committed:
    reference: str
    order_unit_id: str
    action: str
    replacement_sku_id: str | None
    repeated: bool  # True when this call found an earlier commit (a retry)


def _existing(conn, proposal_id) -> Committed:
    a = conn.execute(
        text("SELECT reference, order_unit_id, action::text AS action, replacement_sku_id "
             "FROM actions WHERE proposal_id = :p"),
        {"p": proposal_id},
    ).one()
    return Committed(a.reference, a.order_unit_id, a.action, a.replacement_sku_id, True)


def commit_action(ctx: Ctx, proposal_id: uuid.UUID) -> Committed:
    failure: Refusal | None = None
    try:
        with ctx.engine.begin() as conn:
            # 1. Lock the proposal. A concurrent commit of it waits here.
            p = conn.execute(
                text("""
                    SELECT id, order_unit_id, action::text AS action, replacement_sku_id,
                           status::text AS status
                    FROM proposals WHERE id = :p AND session_id = :s FOR UPDATE
                """),
                {"p": proposal_id, "s": ctx.session_id},
            ).first()
            if p is None:
                raise Refusal("not_found", "no such proposal in this session")

            # 2. Already committed: return the same result, change nothing.
            if p.status == "committed":
                return _existing(conn, proposal_id)

            # 3. Only the shopper's click makes a commit legal.
            if p.status == "open":
                raise Refusal("not_confirmed", "the shopper has not confirmed this proposal")
            if p.status != "confirmed":
                raise Refusal("stale_proposal", f"proposal is {p.status}")

            # 4. Recheck policy with today's date and current stock.
            unit = get_unit(conn, ctx, p.order_unit_id)
            size = None
            if p.replacement_sku_id:
                size = conn.execute(
                    text("SELECT size FROM skus WHERE id = :k"), {"k": p.replacement_sku_id}
                ).scalar()
            decision = decide(conn, ctx, unit, Action(p.action), size)
            if not decision.eligible:
                failure = Refusal(
                    decision.reason.value,
                    data={"sizes_in_stock": decision.sizes_in_stock,
                          "existing_reference": unit.active_reference},
                )

            # 5. Take the stock. Only one of two racing shoppers can succeed here.
            if failure is None and p.replacement_sku_id:
                taken = conn.execute(
                    text("UPDATE skus SET stock = stock - 1 WHERE id = :k AND stock > 0 "
                         "RETURNING id"),
                    {"k": p.replacement_sku_id},
                ).first()
                if taken is None:
                    failure = Refusal("out_of_stock", data={"sizes_in_stock": []})

            if failure is not None:
                # Keep the 'expired' mark: this transaction commits, then we refuse.
                conn.execute(text("UPDATE proposals SET status = 'expired' WHERE id = :p"),
                             {"p": proposal_id})
            else:
                # 6. Record the action and close the proposal.
                reference = new_reference()
                conn.execute(
                    text("""
                        INSERT INTO actions (reference, proposal_id, order_unit_id, action,
                                             replacement_sku_id)
                        VALUES (:r, :p, :u, :a, :k)
                    """),
                    {"r": reference, "p": proposal_id, "u": p.order_unit_id, "a": p.action,
                     "k": p.replacement_sku_id},
                )
                conn.execute(text("UPDATE proposals SET status = 'committed' WHERE id = :p"),
                             {"p": proposal_id})
                return Committed(reference, p.order_unit_id, p.action,
                                 p.replacement_sku_id, False)
    except IntegrityError as e:
        # Another session committed an action on this unit between our check and insert.
        if "one_active_action_per_unit" in str(e.orig):
            raise Refusal("already_actioned", "another action on this unit won the race") from e
        raise
    raise failure


@dataclass(frozen=True)
class ActionStatus:
    order_unit_id: str
    action: str | None
    reference: str | None


def get_action_status(ctx: Ctx, unit_id: str) -> ActionStatus:
    """What, if anything, is active on this unit. Call it before retrying an uncertain commit."""
    with ctx.engine.connect() as conn:
        unit = get_unit(conn, ctx, unit_id)
    return ActionStatus(unit_id, unit.active_action, unit.active_reference)
