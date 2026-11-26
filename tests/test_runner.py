"""Simulated shopper rules, and the runner end to end with a fake model."""

import pytest

from app.assistants.workflow import WorkflowAssistant
from app.llm import FakeLLM
from evals.runner import run_one
from evals.scenario import Expected, Scenario, ShopperFacts, load_split
from evals.shopper import SimulatedShopper

FACTS = ShopperFacts(means_units=["u_amira_blue"], describes_item_as="the blue Oxford shirt",
                     wants="exchange", size="M", if_unavailable="return")
GOOD = {"order_unit_id": "u_amira_blue", "action": "exchange", "replacement_size": "M"}


def test_shopper_confirms_only_what_it_wants():
    s = SimulatedShopper(FACTS, "hi")
    assert s.respond("confirm", GOOD).kind == "confirm"
    for wrong in ({**GOOD, "replacement_size": "L"}, {**GOOD, "order_unit_id": "u_amira_white"},
                  {**GOOD, "action": "return", "replacement_size": None}):
        assert s.respond("confirm", wrong).kind == "say"


def test_shopper_accepts_its_fallback_after_out_of_stock():
    s = SimulatedShopper(FACTS, "hi")
    assert "return" in s.respond("choose_alternative", None).text
    assert s.respond("confirm", {**GOOD, "action": "return",
                                 "replacement_size": None}).kind == "confirm"


def test_shopper_answers_from_facts_and_stops_when_nothing_is_asked():
    s = SimulatedShopper(FACTS, "hi")
    assert s.respond("which_item", None).text == "the blue Oxford shirt"
    assert "M" in s.respond("which_size", None).text
    assert s.respond(None, None).kind == "stop"


def scenario(**over):
    base = next(s for s in load_split("dev") if s.id == "exch-blue-medium")
    return base.model_copy(update=over)


def assistants(*replies):
    llm = FakeLLM(list(replies))
    return {"workflow": lambda: WorkflowAssistant(llm)}


def test_runner_happy_path_passes(db):
    row = run_one(db, scenario(), "workflow",
                  assistants({"intent": "exchange", "items": [1], "size": "M"}))
    assert row["outcome_correct"] and row["completed"] and not row["notes"]
    assert (row["shopper_turns"], row["ended"], row["tool_calls"]) == (2, "assistant_finished", 4)
    assert row["config_version"] == "workflow-v2+tools-v1+fake@test"
    kinds = [(t["type"], t["who"]) for t in row["transcript"]]
    assert kinds[0] == ("message", "shopper") and ("tool", "commit_action") in kinds


def test_runner_grades_a_misread_conversation_as_failed_without_a_wrong_commit(db):
    # The model misreads "medium" as L (out of stock); the shopper asks for a return
    # instead, and the model misreads that as "nothing". Ends with no action.
    row = run_one(db, scenario(), "workflow", assistants(
        {"intent": "exchange", "items": [1], "size": "L"},
        {"intent": "nothing"}))
    assert not row["wrong_commit"] and not row["outcome_correct"]
    assert any("outcome" in n for n in row["notes"])


def test_runner_stops_at_the_turn_limit(db):
    # Amira has two shirts; the model never understands which one is meant.
    stuck = [{"intent": "exchange"}] + [{} for _ in range(10)]
    row = run_one(db, scenario(), "workflow", assistants(*stuck))
    assert row["ended"] == "turn_limit" and not row["outcome_correct"]


@pytest.mark.parametrize("split", ["dev"])
def test_every_scenario_loads(split):
    assert all(isinstance(s, Scenario) and isinstance(s.expected, Expected)
               for s in load_split(split))


def test_shopper_stops_once_it_has_a_reference():
    s = SimulatedShopper(FACTS, "hi")
    assert s.respond("other", None, "Done, reference RX-ABC234. Anything else?").kind == "stop"
    assert s.respond("other", None, "Anything else?").kind == "say"


@pytest.mark.parametrize(("text", "kind"), [
    ("Large is out of stock. Would you like medium instead?", "choose_alternative"),
    ("Would you like me to pass this to a person to review?", "offer_review"),
    ("Which size would you like?", "which_size"),
    ("Do you want to return it or exchange it?", "which_action"),
    ("Which one do you mean, the blue or the white?", "which_item"),
    ("Please press Confirm when you're ready.", "confirm"),
    ("How can I help?", "other"),
])
def test_shopper_reads_untagged_questions(text, kind):
    from evals.shopper import read_question
    assert read_question(text) == kind


def test_shopper_answers_an_untagged_alternatives_question_with_its_fallback():
    s = SimulatedShopper(FACTS, "hi")
    move = s.respond("other", None, "Large is out of stock. Would you like M instead?")
    assert "return" in move.text   # FACTS fallback is a return
