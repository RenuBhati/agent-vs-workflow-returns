"""The fixed workflow assistant.

Each shopper message gets exactly one model call, which turns the message into
structured fields (extract). Everything after that is plain Python: merge the
fields into the session state, then take the next fixed step. The model never
chooses a tool. Every tool still goes through call_tool, the same logged path
the agent uses, so both assistants are measured the same way.

State (sessions.state) keys:
  items      numbered list shown to the model; identical units share one number
  item       chosen item number, or None
  action     "return" | "exchange" | None
  size       requested replacement size, or None
  awaiting   the question kind we last asked, or None
  blocked    policy reason that stopped us (for the review offer), or None
  done       True once the conversation has reached an end
"""

import json
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.assistants.base import Reply, load_state, save_state, say
from app.llm import LLM, record_call
from app.services.context import Ctx
from app.services.messages import add_message
from app.tools import call_tool

NAME = "workflow"
PROMPT_VERSION = "workflow-v1"


class Extraction(BaseModel):
    """What the model reads out of one shopper message. Every field may be empty."""

    intent: Literal["return", "exchange", "review", "nothing"] | None = None
    items: list[int] = []          # item numbers from the list the model was shown
    size: str | None = None        # e.g. "M"; for an exchange
    yes: bool | None = None        # an answer to a yes/no question
    wants_person: bool = False


EXTRACT_SCHEMA = Extraction.model_json_schema()

SYSTEM = """You read one message from a shopper talking to a clothing shop's returns desk.
Fill in the JSON fields from the message only. Leave a field null or empty if the message
does not say it. Do not guess.
- intent: "return" (wants money back), "exchange" (wants a different size), "review"
  (wants a person, disputes a rule, or claims an exception), "nothing" (does not want
  to go ahead), or null.
- items: the numbers of the items in the list below that the message refers to. Use
  colour, item name and size to match. Empty if unclear.
- size: the size they want for an exchange, as written in the shop (XS, S, M, L, XL).
- yes: true or false only if the message answers the last question with yes or no.
- wants_person: true if they ask for a person, a human, or a manager."""

# Policy reasons that end in a review offer, and the review reason to file.
REVIEW_FOR = {
    "final_sale": "final_sale_dispute",
    "needs_review": "no_delivery_record",
    "expired": "policy_exception",
    "already_actioned": "action_conflict",
}

EXPLAIN = {
    "final_sale": "{item} was bought as final sale, so it can't be returned or exchanged.",
    "needs_review": "I can't find a delivery record for {item}, so I can't process it here.",
    "expired": "{item} is past its 30-day window (the last day was {last_day}).",
    "already_actioned": "{item} already has a {active_action} in progress "
                        "(reference {active_reference}).",
}


# ---------------------------------------------------------------- items

def _group_items(units: list[dict]) -> list[dict]:
    """Number the shopper's items. Identical units (same order, style, size) share one
    number, so the shopper is never asked to choose between two copies of a shirt."""
    groups: dict[tuple, dict] = {}
    for u in units:
        key = (u["order_id"], u["item"], u["colour"], u["size"], u["final_sale"])
        g = groups.setdefault(key, {
            "label": f'{u["item"]} ({u["colour"]}), size {u["size"]}, order {u["order_id"]}',
            "units": []})
        g["units"].append(u)
    return [{"n": i + 1, **g} for i, g in enumerate(groups.values())]


def _item(state: dict) -> dict | None:
    return next((g for g in state["items"] if g["n"] == state.get("item")), None)


def _unit(group: dict) -> dict:
    """Prefer a copy with nothing already in progress."""
    return next((u for u in group["units"] if not u["active_action"]), group["units"][0])


def _items_text(state: dict) -> str:
    return "\n".join(f'{g["n"]}. {g["label"]}' for g in state["items"])


# ---------------------------------------------------------------- the assistant

class WorkflowAssistant:
    name = NAME

    def __init__(self, llm: LLM):
        self.llm = llm

    # -- one shopper message
    def handle(self, ctx: Ctx, message: str, turn: int) -> Reply:
        add_message(ctx, "shopper", message)
        state = load_state(ctx)
        if not state:
            r = call_tool(ctx, "list_orders", {}, turn)
            state = {"items": _group_items(r.data["units"]), "item": None, "action": None,
                     "size": None, "awaiting": None, "blocked": None, "done": False}
            if len(state["items"]) == 1:
                state["item"] = 1
        if state["done"]:
            return self._finish(ctx, state, "Is there anything else? Start a new chat for "
                                "another item.", None)

        ex = self._extract(ctx, state, message, turn)
        if ex is None:
            save_state(ctx, state)
            return say(ctx, "Sorry, I didn't catch that. Could you say it another way?",
                       state["awaiting"] or "other")

        reply = self._merge(ctx, state, ex, turn)
        if reply is None:
            reply = self._next_step(ctx, state, turn)
        save_state(ctx, state)
        return reply

    # -- the shopper pressed Confirm (the API has already recorded the click)
    def on_confirm(self, ctx: Ctx, proposal_id: str, turn: int) -> Reply:
        state = load_state(ctx)
        r = call_tool(ctx, "commit_action", {"proposal_id": proposal_id}, turn)
        if r.status == "ok":
            ref = r.data["reference"]
            what = "return" if r.data["action"] == "return" else "exchange"
            return self._finish(ctx, state, f"Done. Your {what} is booked. Your reference is "
                                f"{ref}.", None)
        if r.reason == "out_of_stock":
            state["size"], state["awaiting"] = None, None
            reply = say(ctx, "Sorry, the last one in that size has just sold out. "
                        + self._alternatives_text(ctx, state, turn), "choose_alternative")
            state["awaiting"] = "choose_alternative"
            save_state(ctx, state)
            return reply
        return self._finish(ctx, state, "Sorry, I couldn't complete that, and nothing has "
                            f"been changed (reason: {r.reason}).", None)

    # ------------------------------------------------------------ steps

    def _extract(self, ctx: Ctx, state: dict, message: str, turn: int) -> Extraction | None:
        last_q = state["awaiting"] or "none"
        user = (f"The shopper's items:\n{_items_text(state)}\n\n"
                f"Last question asked: {last_q}\n\nShopper's message: {message}")
        reply = self.llm.chat([{"role": "system", "content": SYSTEM},
                               {"role": "user", "content": user}], schema=EXTRACT_SCHEMA)
        try:
            ex = None if reply.error else Extraction.model_validate(json.loads(reply.content))
        except (ValueError, ValidationError):
            reply.error = "invalid_extraction"
            ex = None
        record_call(ctx, self.llm, turn, "extract", reply)
        return ex

    def _merge(self, ctx: Ctx, state: dict, ex: Extraction, turn: int) -> Reply | None:
        """Apply what the shopper said. Returns a reply only when the message ends things."""
        awaiting = state["awaiting"]
        valid = [n for n in ex.items if any(g["n"] == n for g in state["items"])]

        if ex.wants_person or ex.intent == "review":
            reason = REVIEW_FOR.get(state["blocked"], "shopper_request")
            return self._review(ctx, state, reason, turn)

        if awaiting == "offer_review":
            if ex.yes is True:
                return self._review(ctx, state, REVIEW_FOR[state["blocked"]], turn)
            if ex.yes is False or ex.intent == "nothing":
                return self._finish(ctx, state, "OK. Nothing has been changed.", None)

        if ex.intent == "nothing" or (ex.yes is False and awaiting in
                                      ("confirm", "choose_alternative")):
            return self._finish(ctx, state, "No problem. Nothing has been changed.", None)

        if len(valid) == 1:
            if state["item"] not in (None, valid[0]):
                state["size"] = None
            state["item"] = valid[0]
        if ex.intent in ("return", "exchange"):
            state["action"] = ex.intent
        if ex.size:
            state["size"] = ex.size.strip().upper()
            if awaiting == "choose_alternative" and ex.intent is None:
                state["action"] = "exchange"
        if state["action"] == "return":
            state["size"] = None
        state["blocked"] = None
        return None

    def _next_step(self, ctx: Ctx, state: dict, turn: int) -> Reply:
        group = _item(state)
        if group is None:
            return self._ask(ctx, state, "Which item do you mean?\n" + _items_text(state),
                             "which_item")
        if state["action"] is None:
            return self._ask(ctx, state, f'Would you like to return or exchange the '
                             f'{group["label"]}?', "which_action")

        unit = _unit(group)
        uid = unit["order_unit_id"]
        r = call_tool(ctx, "check_options", {"order_unit_id": uid, "action": state["action"],
                                             "size": state["size"]}, turn)
        if r.status != "ok":
            return self._finish(ctx, state, "Sorry, something went wrong looking that up. "
                                "Nothing has been changed.", None)
        opt = r.data

        if opt["reason"] in REVIEW_FOR:
            state["blocked"] = opt["reason"]
            text_ = EXPLAIN[opt["reason"]].format(**{
                **opt["unit"], "item": f'The {group["label"]}',
                "last_day": opt["unit"]["last_day_to_return"]})
            return self._ask(ctx, state, text_ + " Would you like me to pass it to a person "
                             "to review?", "offer_review")
        if opt["reason"] in ("out_of_stock", "same_size", "unknown_size"):
            why = {"out_of_stock": f'Size {state["size"]} is out of stock.',
                   "same_size": f'You already have size {state["size"]}.',
                   "unknown_size": f'That item doesn\'t come in size {state["size"]}.'}
            state["size"] = None
            return self._ask(ctx, state, why[opt["reason"]] + " "
                             + self._alternatives_text(ctx, state, turn, opt),
                             "choose_alternative")
        if state["action"] == "exchange" and state["size"] is None:
            sizes = ", ".join(opt["sizes_in_stock"]) or "none"
            if not opt["sizes_in_stock"]:
                return self._ask(ctx, state, "There are no other sizes in stock. Would you "
                                 "like to return it instead?", "choose_alternative")
            return self._ask(ctx, state, f"Which size would you like? In stock: {sizes}.",
                             "which_size")

        p = call_tool(ctx, "propose", {"order_unit_id": uid, "action": state["action"],
                                       "replacement_size": state["size"]}, turn)
        if p.status != "ok":
            return self._finish(ctx, state, "Sorry, I couldn't set that up. Nothing has been "
                                "changed.", None)
        return self._ask(ctx, state, p.data["summary"] + " Press Confirm to go ahead.",
                         "confirm")

    # ------------------------------------------------------------ helpers

    def _alternatives_text(self, ctx: Ctx, state: dict, turn: int,
                           opt: dict | None = None) -> str:
        if opt is None:
            group = _item(state)
            opt = call_tool(ctx, "check_options", {"order_unit_id": _unit(group)["order_unit_id"],
                                                   "action": "exchange"}, turn).data
        sizes = opt.get("sizes_in_stock") or []
        if sizes:
            return (f"In stock: {', '.join(sizes)}. Would you like one of those, a return "
                    "instead, or to leave it?")
        return "No other sizes are in stock. Would you like a return instead, or to leave it?"

    def _review(self, ctx: Ctx, state: dict, reason: str, turn: int) -> Reply:
        group = _item(state)
        args = {"reason": reason, "question": f"Shopper asked for review ({reason})."}
        if group:
            args["order_unit_id"] = _unit(group)["order_unit_id"]
        r = call_tool(ctx, "request_review", args, turn)
        if r.status != "ok":
            return self._finish(ctx, state, "Sorry, I couldn't pass this on. Nothing has "
                                "been changed.", None)
        return self._finish(ctx, state, "I've passed this to a person, who will look at it "
                            "and get back to you. Nothing on your order has changed.", None)

    def _ask(self, ctx: Ctx, state: dict, text_: str, kind) -> Reply:
        state["awaiting"] = kind
        return say(ctx, text_, kind)

    def _finish(self, ctx: Ctx, state: dict, text_: str, kind) -> Reply:
        state["awaiting"], state["done"] = None, True
        save_state(ctx, state)
        return say(ctx, text_, kind)
