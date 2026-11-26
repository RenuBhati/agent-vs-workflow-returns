"""Scenario files: one conversation to run, the shopper's private facts, and the
expected end state. Renu writes the expected outcomes; never the code's author.
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from app.fixtures import read_fixture
from app.policy import Action
from app.services.review import ReviewReason

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"

ClarifyKind = Literal["which_item", "which_size"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ShopperFacts(Strict):
    """What the simulated shopper knows. Never shown to the assistant."""

    means_units: list[str]                 # the item(s) the shopper is talking about
    describes_item_as: str                 # answer to "which item?"
    wants: Action | Literal["review", "nothing"]
    size: str | None = None                # answer to "which size?"
    if_unavailable: Literal["return", "nothing", "review"] | str = "nothing"  # or "size:L"
    accepts_review: bool = True


class Expected(Strict):
    outcome: Literal["action", "review", "no_action"]
    acceptable_units: list[str] = []       # any of these is correct (identical units)
    action: Action | None = None
    replacement_size: str | None = None
    stock_delta: dict[str, int] = {}       # exact; every other sku must not change
    review_reason: ReviewReason | None = None
    needs_clarification: list[ClarifyKind] = []

    @model_validator(mode="after")
    def consistent(self) -> "Expected":
        if self.outcome == "action":
            if not self.acceptable_units or self.action is None:
                raise ValueError("an action outcome needs acceptable_units and action")
            if (self.action is Action.EXCHANGE) != (self.replacement_size is not None):
                raise ValueError("replacement_size is required for, and only for, exchanges")
        elif self.action or self.replacement_size or self.stock_delta:
            raise ValueError(f"a {self.outcome} outcome cannot change orders or stock")
        if self.outcome != "review" and self.review_reason:
            raise ValueError("review_reason only applies to a review outcome")
        return self


class Scenario(Strict):
    id: str
    split: Literal["dev", "heldout"]
    fixture: str
    persona: str
    opening: str
    wordings: list[str] = []               # alternate phrasings of the opening message
    shopper_facts: ShopperFacts
    expected: Expected
    notes: str = ""


def load_scenario(path: Path) -> Scenario:
    s = Scenario.model_validate(yaml.safe_load(path.read_text()))
    _check_against_fixture(s)
    return s


def load_split(split: str) -> list[Scenario]:
    return [load_scenario(p) for p in sorted((SCENARIO_DIR / split).glob("*.yaml"))]


def _check_against_fixture(s: Scenario) -> None:
    """Catch typos in unit and sku IDs before a run is wasted on them."""
    fx = read_fixture(s.fixture)
    personas = {sh.persona: sh for sh in fx.shoppers}
    if s.persona not in personas:
        raise ValueError(f"{s.id}: persona {s.persona!r} not in {s.fixture}")
    all_units = {u.id for sh in fx.shoppers for o in sh.orders for u in o.units}
    skus = {k.id for k in fx.skus}
    for u in s.shopper_facts.means_units + s.expected.acceptable_units:
        if u not in all_units:
            raise ValueError(f"{s.id}: unknown unit {u!r}")
    for k in s.expected.stock_delta:
        if k not in skus:
            raise ValueError(f"{s.id}: unknown sku {k!r}")
