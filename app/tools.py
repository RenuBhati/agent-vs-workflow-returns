"""The six tools both assistants use, and the one function that runs them.

The agent loop shows TOOLS to the model; the fixed workflow calls call_tool()
directly. Either way every call goes through call_tool(), which validates the
arguments, runs the service, shapes the result as JSON and logs the call in
tool_calls.

confirm and decline are deliberately missing: only the shopper's button can
confirm, through the API. The model can propose and commit, never confirm.

TOOLSET_VERSION is recorded on each session. The harness experiment
(milestone 5) changes the descriptions here and bumps the version.
"""

import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text

from app.policy import Action
from app.services.actions import commit_action, get_action_status
from app.services.context import Ctx
from app.services.errors import Refusal
from app.services.orders import UnitView, check_options, list_orders
from app.services.proposals import propose
from app.services.review import ReviewReason, request_review

TOOLSET_VERSION = "tools-v1"

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- arguments

class Args(BaseModel):
    # An argument the model invents (e.g. shopper_id) is an error, not ignored.
    model_config = ConfigDict(extra="forbid")


class NoArgs(Args):
    pass


class CheckOptionsArgs(Args):
    order_unit_id: str = Field(description="The unit's id from list_orders, e.g. u_amira_blue.")
    action: Action
    size: str | None = Field(None, description="For an exchange: the size the shopper wants.")


class ProposeArgs(Args):
    order_unit_id: str
    action: Action
    replacement_size: str | None = Field(None, description="Required for an exchange.")


class CommitArgs(Args):
    proposal_id: uuid.UUID


class UnitArgs(Args):
    order_unit_id: str


class ReviewArgs(Args):
    reason: ReviewReason
    question: str = Field(description="The shopper's issue in one or two sentences.")
    order_unit_id: str | None = None


# ---------------------------------------------------------------- results

def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _unit(u: UnitView) -> dict:
    return {
        "order_unit_id": u.unit_id, "order_id": u.order_id, "item": u.name,
        "colour": u.colour, "size": u.size, "delivered_on": _iso(u.delivered_on),
        "last_day_to_return": _iso(u.last_day), "final_sale": u.final_sale,
        "active_action": u.active_action, "active_reference": u.active_reference,
    }


def _list_orders(ctx: Ctx, a: NoArgs) -> dict:
    return {"units": [_unit(u) for u in list_orders(ctx)]}


def _check_options(ctx: Ctx, a: CheckOptionsArgs) -> dict:
    unit, d = check_options(ctx, a.order_unit_id, a.action, a.size)
    return {"unit": _unit(unit), "eligible": d.eligible,
            "reason": d.reason.value if d.reason else None,
            "sizes_in_stock": d.sizes_in_stock}


def _propose(ctx: Ctx, a: ProposeArgs) -> dict:
    p = propose(ctx, a.order_unit_id, a.action, a.replacement_size)
    return {"proposal_id": str(p.id), "version": p.version, "summary": p.summary,
            "status": p.status}


def _commit(ctx: Ctx, a: CommitArgs) -> dict:
    c = commit_action(ctx, a.proposal_id)
    return {"reference": c.reference, "order_unit_id": c.order_unit_id, "action": c.action,
            "already_committed": c.repeated}


def _status(ctx: Ctx, a: UnitArgs) -> dict:
    s = get_action_status(ctx, a.order_unit_id)
    return {"order_unit_id": s.order_unit_id, "action": s.action, "reference": s.reference}


def _review(ctx: Ctx, a: ReviewArgs) -> dict:
    case = request_review(ctx, a.reason, a.question, a.order_unit_id)
    return {"case_id": str(case.id), "existing_case": case.reused}


# ---------------------------------------------------------------- definitions

@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[Args]
    run: Callable[[Ctx, Any], dict]
    writes: bool  # whether a refusal counts as an attempted invalid write

    def schema(self) -> dict:
        """Provider-neutral: name, description, JSON Schema for the input."""
        s = self.args.model_json_schema()
        s.pop("title", None)
        return {"name": self.name, "description": self.description, "input_schema": s}


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("list_orders",
         "List every item the shopper bought, with its order, size, delivery date, last day "
         "to return, whether it was final sale, and any return or exchange already made. "
         "Call this first to find the order_unit_id of the item the shopper means.",
         NoArgs, _list_orders, writes=False),
    Tool("check_options",
         "Check whether one item can be returned or exchanged today, and which other sizes "
         "are in stock. Changes nothing. Use it before proposing.",
         CheckOptionsArgs, _check_options, writes=False),
    Tool("propose",
         "Create the proposal the shopper will be asked to confirm. Replaces any earlier "
         "proposal in this conversation. The shopper confirms with a button; you cannot "
         "confirm for them. Refused if the item is not eligible.",
         ProposeArgs, _propose, writes=True),
    Tool("commit_action",
         "Carry out a proposal after the shopper has confirmed it. Returns the reference to "
         "give the shopper. Refused if the shopper has not confirmed this exact proposal. "
         "Only tell the shopper it is done after this returns a reference.",
         CommitArgs, _commit, writes=True),
    Tool("get_action_status",
         "Look up any return or exchange already made on an item. Use it if a commit's "
         "result is unclear, before trying again.",
         UnitArgs, _status, writes=False),
    Tool("request_review",
         "Hand the case to a person. Use for disputes, missing delivery records, claimed "
         "exceptions, conflicts with an existing action, or when the shopper asks for a "
         "person. Changes nothing on the order.",
         ReviewArgs, _review, writes=True),
]}


def tool_schemas() -> list[dict]:
    return [t.schema() for t in TOOLS.values()]


# ---------------------------------------------------------------- running

Status = Literal["ok", "refused", "error"]


@dataclass(frozen=True)
class ToolResult:
    status: Status
    data: dict
    reason: str | None = None

    def as_dict(self) -> dict:
        out: dict = {"status": self.status, "data": self.data}
        if self.reason:
            out["reason"] = self.reason
        return out


def _log(ctx: Ctx, turn: int, name: str, args: dict, result: ToolResult, ms: int) -> None:
    # Separate transaction: a refused write rolled back its own changes, but the
    # record that it was attempted must survive.
    with ctx.engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO tool_calls (session_id, trace_id, turn, tool, args,
                                        result_status, reason_code, duration_ms)
                VALUES (:s, :t, :turn, :tool, CAST(:args AS jsonb), :st, :rc, :ms)
            """),
            {"s": ctx.session_id, "t": ctx.trace_id, "turn": turn, "tool": name,
             "args": _json(args), "st": result.status, "rc": result.reason, "ms": ms},
        )


def _json(value: Any) -> str:
    return json.dumps(value, default=str)


def call_tool(ctx: Ctx, name: str, args: dict | None, turn: int) -> ToolResult:
    args = args or {}
    started = time.perf_counter()
    tool = TOOLS.get(name)
    if tool is None:
        result = ToolResult("error", {"known_tools": list(TOOLS)}, "unknown_tool")
    else:
        try:
            parsed = tool.args.model_validate(args)
            result = ToolResult("ok", tool.run(ctx, parsed))
        except ValidationError as e:
            problems = [{"field": ".".join(map(str, err["loc"])), "problem": err["msg"]}
                        for err in e.errors()]
            result = ToolResult("error", {"problems": problems}, "invalid_args")
        except Refusal as r:
            result = ToolResult("refused", r.data, r.code)
        except Exception as e:  # logged in full below; the model gets no internals
            log.exception("tool %s failed (session %s, trace %s)", name, ctx.session_id,
                          ctx.trace_id)
            result = ToolResult("error", {"error_type": type(e).__name__}, "internal_error")
    ms = round((time.perf_counter() - started) * 1000)
    _log(ctx, turn, name, args, result, ms)
    return result
