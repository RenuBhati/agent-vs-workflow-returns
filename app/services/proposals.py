"""Proposals: what the shopper is asked to confirm.

Each version is its own row. A correction supersedes the live version and
inserts version + 1 in the same transaction. confirm() records the shopper's
click and nothing else; committing is a separate step.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text

from app.policy import Action
from app.services.context import Ctx
from app.services.errors import Refusal
from app.services.orders import UnitView, decide, get_unit


@dataclass(frozen=True)
class Proposal:
    id: uuid.UUID
    version: int
    order_unit_id: str
    action: str
    replacement_size: str | None
    status: str
    summary: str


def _summary(unit: UnitView, action: Action, size: str | None) -> str:
    item = f"{unit.name} ({unit.colour}), size {unit.size}, from order {unit.order_id}"
    if action is Action.EXCHANGE:
        return f"Exchange {item} for size {size}."
    return f"Return {item}."


def propose(ctx: Ctx, unit_id: str, action: Action,
            replacement_size: str | None = None) -> Proposal:
    if action is Action.EXCHANGE and replacement_size is None:
        raise Refusal("size_required", "an exchange proposal needs a replacement size")
    if action is Action.RETURN:
        replacement_size = None

    with ctx.engine.begin() as conn:
        unit = get_unit(conn, ctx, unit_id)
        decision = decide(conn, ctx, unit, action, replacement_size)
        if not decision.eligible:
            raise Refusal(
                decision.reason.value,
                data={"sizes_in_stock": decision.sizes_in_stock,
                      "existing_reference": unit.active_reference},
            )

        # Lock the session's live proposal (if any) and retire it.
        conn.execute(
            text("""
                UPDATE proposals SET status = 'superseded'
                WHERE session_id = :s AND status IN ('open', 'confirmed')
            """),
            {"s": ctx.session_id},
        )
        version = conn.execute(
            text("SELECT coalesce(max(version), 0) + 1 FROM proposals WHERE session_id = :s"),
            {"s": ctx.session_id},
        ).scalar()
        sku_id = (
            conn.execute(
                text("SELECT id FROM skus WHERE style_id = :st AND size = :sz"),
                {"st": unit.style_id, "sz": replacement_size},
            ).scalar()
            if replacement_size
            else None
        )
        summary = _summary(unit, action, replacement_size)
        pid = conn.execute(
            text("""
                INSERT INTO proposals (session_id, order_unit_id, action, replacement_sku_id,
                                       version, summary)
                VALUES (:s, :u, :a, :k, :v, :sm) RETURNING id
            """),
            {"s": ctx.session_id, "u": unit_id, "a": action.value, "k": sku_id,
             "v": version, "sm": summary},
        ).scalar()
    return Proposal(pid, version, unit_id, action.value, replacement_size, "open", summary)


def _lock_own(conn, ctx: Ctx, proposal_id: uuid.UUID):
    row = conn.execute(
        text("""
            SELECT id, version, status::text AS status FROM proposals
            WHERE id = :p AND session_id = :s FOR UPDATE
        """),
        {"p": proposal_id, "s": ctx.session_id},
    ).first()
    if row is None:
        raise Refusal("not_found", "no such proposal in this session")
    return row


def confirm(ctx: Ctx, proposal_id: uuid.UUID, version: int) -> str:
    """The shopper clicked Confirm on this exact version. Safe to repeat."""
    with ctx.engine.begin() as conn:
        p = _lock_own(conn, ctx, proposal_id)
        if p.version != version:
            raise Refusal("stale_proposal", f"shown version {version}, stored {p.version}")
        if p.status == "confirmed":
            return "confirmed"  # a double click changes nothing
        if p.status != "open":
            raise Refusal("stale_proposal", f"proposal is {p.status}")
        conn.execute(
            text("UPDATE proposals SET status = 'confirmed', confirmed_at = now() WHERE id = :p"),
            {"p": proposal_id},
        )
    return "confirmed"


def decline(ctx: Ctx, proposal_id: uuid.UUID) -> str:
    with ctx.engine.begin() as conn:
        p = _lock_own(conn, ctx, proposal_id)
        if p.status not in ("open", "confirmed"):
            raise Refusal("stale_proposal", f"proposal is {p.status}")
        conn.execute(
            text("UPDATE proposals SET status = 'declined' WHERE id = :p"), {"p": proposal_id}
        )
    return "declined"
