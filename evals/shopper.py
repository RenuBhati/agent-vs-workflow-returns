"""The simulated shopper: rule-based, no model.

It answers each tagged question from its private facts, and presses Confirm
only if the proposal is what it wants. Both assistants face exactly this
shopper, so differences in results come from the assistant.

It never sees the expected outcome, only ShopperFacts.
"""

import re
from dataclasses import dataclass

from evals.scenario import ShopperFacts

REFERENCE = re.compile(r"\bRX-[A-Z0-9]{6}\b")


@dataclass(frozen=True)
class Move:
    kind: str            # "say" | "confirm" | "stop"
    text: str = ""


class SimulatedShopper:
    def __init__(self, facts: ShopperFacts, opening: str):
        self.facts = facts
        self.opening = opening
        # What the shopper would accept. Starts as what it wants; an accepted
        # alternative (after "that's out of stock") changes it.
        self.accept_action = facts.wants
        self.accept_size = facts.size

    def first(self) -> Move:
        return Move("say", self.opening)

    def respond(self, kind: str | None, proposal: dict | None, text: str = "") -> Move:
        f = self.facts
        # A shopper who has been given a reference is done, whatever else is asked
        # ("Anything else?"). Repeating the request would only invite a second action.
        if kind is None or (kind in ("other", "none") and REFERENCE.search(text)):
            return Move("stop")
        if kind == "confirm":
            if proposal and self._wanted(proposal):
                return Move("confirm")
            return Move("say", f"No, that's not right. I meant {f.describes_item_as}"
                        + (f", size {self.accept_size}" if self.accept_action == "exchange"
                           and self.accept_size else "") + ".")
        if kind == "which_item":
            return Move("say", f.describes_item_as)
        if kind == "which_size":
            if self.accept_size:
                return Move("say", f"Size {self.accept_size}, please.")
            return Move("say", "I'm not sure, what do you have?")
        if kind == "which_action":
            if f.wants in ("return", "exchange"):
                return Move("say", f"I'd like to {f.wants} it.")
            return Move("say", "I'd like a person to look at it.")
        if kind == "choose_alternative":
            alt = f.if_unavailable
            if alt == "return":
                self.accept_action, self.accept_size = "return", None
                return Move("say", "A return instead, please.")
            if alt == "review":
                return Move("say", "Can a person look at this, please?")
            if alt.startswith("size:"):
                self.accept_action, self.accept_size = "exchange", alt.split(":", 1)[1]
                return Move("say", f"Size {self.accept_size} then, please.")
            return Move("say", "No thanks, I'll leave it.")
        if kind == "offer_review":
            return Move("say", "Yes, please." if f.accepts_review else "No, thank you.")
        return Move("say", self.opening)   # "other": repeat what it wants

    def _wanted(self, p: dict) -> bool:
        return (p["order_unit_id"] in self.facts.means_units
                and p["action"] == self.accept_action
                and (p["action"] != "exchange" or p["replacement_size"] == self.accept_size))
