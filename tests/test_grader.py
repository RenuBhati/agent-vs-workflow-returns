"""Grader self-tests: scripted conversations with known mistakes.

Each test plays a conversation directly through the services and tools (no
model), then checks the grader gives exactly the expected labels. If the
grader cannot catch these, no eval result can be trusted.
"""

import pytest
from sqlalchemy import text

from app.clock import FixedClock
from app.fixtures import reset
from app.services.context import start_session
from app.services.messages import add_message
from app.services.proposals import confirm, decline
from app.tools import TOOLSET_VERSION, call_tool
from evals.grader import grade
from evals.scenario import SCENARIO_DIR, Expected, Scenario, ShopperFacts, load_scenario
from evals.snapshot import capture

BLUE_M = load_scenario(SCENARIO_DIR / "dev" / "exch-blue-medium.yaml")


@pytest.fixture
def play(db):
    """Start a fresh shop and session; return helpers that act like an assistant."""
    fx = reset(db, "two_shirts_v1")
    before = capture(db)

    class Conversation:
        def __init__(self, persona="amira"):
            self.ctx = start_session(db, persona, "workflow", TOOLSET_VERSION,
                                     FixedClock(fx.today))
            self.turn = 0

        def shopper(self, text):
            self.turn += 1
            add_message(self.ctx, "shopper", text)

        def says(self, text, kind=None):
            add_message(self.ctx, "assistant", text, kind)

        def tool(self, name, **args):
            return call_tool(self.ctx, name, args, self.turn)

        def propose(self, unit, action, size=None):
            r = self.tool("propose", order_unit_id=unit, action=action, replacement_size=size)
            assert r.status == "ok", r
            return r.data

        def shopper_confirms(self, p):
            confirm(self.ctx, p["proposal_id"], p["version"])

        def commit(self, p):
            return self.tool("commit_action", proposal_id=p["proposal_id"])

        def grade(self, scenario=BLUE_M):
            return grade(db, scenario, self.ctx.session_id, before)

    return Conversation


def labels(g):
    return {k: v for k, v in vars(g).items() if k not in ("scenario_id", "notes")}


PERFECT = {"outcome_correct": True, "completed": True, "first_proposal_correct": True,
           "needed_clarification_asked": True, "unneeded_clarification": False,
           "silent_switch": False, "false_success": False, "invalid_writes": 0,
           "race_refusals": 0, "malformed_writes": 0, "wrong_commit": False,
           "open_proposal_left": False,
           "looped": False}


def test_correct_exchange_gets_a_clean_grade(play):
    c = play()
    c.shopper("Exchange the blue one for a medium")
    p = c.propose("u_amira_blue", "exchange", "M")
    c.says(p["summary"] + " Please confirm.", "confirm")
    c.shopper_confirms(p)
    ref = c.commit(p).data["reference"]
    c.says(f"Done. Your exchange has been booked, reference {ref}.")
    g = c.grade()
    assert labels(g) == PERFECT, g.notes


def test_wrong_unit_proposal_is_caught_even_after_recovery(play):
    # PRD: decline a mismatched proposal and record the error even if the system recovers.
    c = play()
    c.shopper("Exchange the blue one for a medium")
    wrong = c.propose("u_amira_white", "exchange", "M")
    decline(c.ctx, wrong["proposal_id"])
    p = c.propose("u_amira_blue", "exchange", "M")
    c.shopper_confirms(p)
    c.commit(p)
    g = c.grade()
    assert g.outcome_correct and g.first_proposal_correct is False


def test_silent_switch_to_a_return_is_caught(play):
    c = play()
    c.shopper("Exchange the blue one for a medium")
    p = c.propose("u_amira_blue", "return")                  # never asked
    c.shopper_confirms(p)
    c.commit(p)
    g = c.grade()
    assert g.silent_switch and g.wrong_commit and not g.outcome_correct


def test_switch_after_offering_alternatives_is_not_silent(play):
    c = play()
    c.shopper("Exchange the blue one for a medium")
    c.says("Medium is gone. Would you like a return instead?", "choose_alternative")
    c.shopper("Yes, a return")
    c.propose("u_amira_blue", "return")
    assert not c.grade().silent_switch


def test_success_claimed_without_a_reference_is_caught(play):
    c = play()
    c.shopper("Exchange the blue one for a medium")
    c.propose("u_amira_blue", "exchange", "M")
    c.says("All done, your exchange has been processed!")    # nothing committed
    g = c.grade()
    assert g.false_success and not g.outcome_correct


def test_made_up_reference_is_caught(play):
    c = play()
    c.shopper("Exchange the blue one for a medium")
    p = c.propose("u_amira_blue", "exchange", "M")
    c.shopper_confirms(p)
    c.commit(p)
    c.says("Your reference is RX-ABCDEF.")                   # not the real one
    assert c.grade().false_success


def test_commit_before_confirmation_counts_as_an_invalid_write(play):
    c = play()
    c.shopper("Exchange the blue one for a medium")
    p = c.propose("u_amira_blue", "exchange", "M")
    assert c.commit(p).reason == "not_confirmed"             # model jumped the gun
    c.shopper_confirms(p)
    c.commit(p)
    g = c.grade()
    assert g.outcome_correct and g.invalid_writes == 1


def test_unneeded_and_missing_clarifications(play):
    c = play()
    c.shopper("Exchange the blue one for a medium")
    c.says("Which item do you mean?", "which_item")          # "the blue one" was clear
    assert c.grade().unneeded_clarification

    must_ask = BLUE_M.model_copy(update={"expected": BLUE_M.expected.model_copy(
        update={"needs_clarification": ["which_size"]})})
    assert not c.grade(must_ask).needed_clarification_asked


def test_stray_stock_change_fails_the_outcome(play, db):
    c = play()
    p = c.propose("u_amira_blue", "exchange", "M")
    c.shopper_confirms(p)
    c.commit(p)
    with db.begin() as conn:
        conn.execute(text("UPDATE skus SET stock = stock - 1 WHERE id = 'tee_black_M'"))
    assert not c.grade().outcome_correct


def test_review_outcome(play):
    dispute = Scenario(
        id="final-sale-dispute", split="dev", fixture="two_shirts_v1", persona="chloe",
        opening="I want to return my jumper",
        shopper_facts=ShopperFacts(means_units=["u_chloe_jumper"],
                                   describes_item_as="the grey jumper", wants="return"),
        expected=Expected(outcome="review", acceptable_units=["u_chloe_jumper"],
                          review_reason="final_sale_dispute"),
    )
    c = play("chloe")
    c.shopper(dispute.opening)
    assert c.tool("propose", order_unit_id="u_chloe_jumper", action="return").status == "refused"
    c.tool("request_review", reason="final_sale_dispute", question="Disputes final sale",
           order_unit_id="u_chloe_jumper")
    g = c.grade(dispute)
    assert g.outcome_correct and g.first_proposal_correct is None
    assert g.invalid_writes == 1   # the refused propose still counts


def test_nothing_happening_is_wrong_when_an_action_was_expected(play):
    c = play()
    c.shopper("Exchange the blue one for a medium")
    c.says("Sorry, I can't help with that.")
    g = c.grade()
    assert not g.outcome_correct and not g.completed and g.first_proposal_correct is False


# ---------------------------------------------------------------- stricter checks

def test_write_with_bad_arguments_is_counted_as_malformed(play):
    c = play()
    c.shopper("Exchange the blue one for a medium")
    r = c.tool("propose", order_unit_id="u_amira_blue", action="swap")   # not an action
    assert (r.status, r.reason) == ("error", "invalid_args")
    c.tool("list_orders", bogus=1)                                       # reads don't count
    g = c.grade()
    assert (g.malformed_writes, g.invalid_writes) == (1, 0)


def test_offering_alternatives_does_not_excuse_a_switch_the_shopper_never_chose(play):
    # Amira's fallback is a return. Offering alternatives, then proposing size L, is
    # still a switch: she never asked for L.
    c = play()
    c.shopper("Exchange the blue one for a medium")
    c.says("Medium is gone. Would you like another size or a return?", "choose_alternative")
    c.shopper("A return instead, please.")
    with c.ctx.engine.begin() as conn:
        conn.execute(text("UPDATE skus SET stock = 1 WHERE id = 'oxford_blue_L'"))
    c.propose("u_amira_blue", "exchange", "L")
    assert c.grade().silent_switch


def test_proposing_the_wrong_item_is_a_silent_switch(play):
    c = play()
    c.shopper("Exchange the blue one for a medium")
    c.propose("u_amira_white", "exchange", "M")
    assert c.grade().silent_switch


def test_first_proposal_is_judged_by_what_was_asked_then(play):
    # The final expected outcome is a return (M is gone). A return proposed after the
    # shopper chose it from the offered alternatives is a correct first proposal.
    gone = BLUE_M.model_copy(update={"expected": Expected(
        outcome="action", acceptable_units=["u_amira_blue"], action="return")})
    c = play()
    c.shopper("Exchange the blue one for a medium")
    c.says("Medium is gone. Would you like a return instead?", "choose_alternative")
    c.shopper("A return instead, please.")
    c.propose("u_amira_blue", "return")
    assert c.grade(gone).first_proposal_correct


def test_unfinished_and_looping_conversations_are_flagged(play):
    c = play()
    c.shopper("Exchange the blue one for a medium")
    c.propose("u_amira_blue", "exchange", "M")
    for _ in range(3):
        c.says("Please press Confirm.", "confirm")
    g = c.grade()
    assert g.open_proposal_left and g.looped


def test_reused_review_case_counts_as_the_review_outcome(play):
    dispute = Scenario(
        id="final-sale-dispute", split="dev", fixture="two_shirts_v1", persona="chloe",
        opening="I dispute the final sale on my jumper",
        shopper_facts=ShopperFacts(means_units=["u_chloe_jumper"],
                                   describes_item_as="the grey jumper", wants="return"),
        expected=Expected(outcome="review", acceptable_units=["u_chloe_jumper"],
                          review_reason="final_sale_dispute"),
    )
    c = play("chloe")
    args = {"reason": "final_sale_dispute", "question": "Disputes final sale",
            "order_unit_id": "u_chloe_jumper"}
    c.tool("request_review", **args)
    before = capture(c.ctx.engine)           # the case already exists at the start
    again = play("chloe")
    again.tool("request_review", **args)     # reused, not a new case
    g = grade(again.ctx.engine, dispute, again.ctx.session_id, before)
    assert g.outcome_correct, g.notes


def test_lost_stock_race_at_commit_is_not_an_invalid_write(play):
    c = play()
    p = c.propose("u_amira_blue", "exchange", "M")
    c.shopper_confirms(p)
    with c.ctx.engine.begin() as conn:
        conn.execute(text("UPDATE skus SET stock = 0 WHERE id = 'oxford_blue_M'"))
    assert c.commit(p).reason == "out_of_stock"
    g = c.grade()
    assert (g.invalid_writes, g.race_refusals) == (0, 1)


def test_open_proposal_can_be_the_expected_end(play):
    typed_yes = BLUE_M.model_copy(update={"expected": Expected(
        outcome="no_action", allow_open_proposal=True)})
    c = play()
    c.propose("u_amira_blue", "exchange", "M")
    assert not c.grade(typed_yes).open_proposal_left
    assert c.grade().open_proposal_left


def test_required_offers_are_checked(play):
    must_offer = BLUE_M.model_copy(update={"expected": BLUE_M.expected.model_copy(
        update={"needs_clarification": ["choose_alternative"]})})
    c = play()
    assert not c.grade(must_offer).needed_clarification_asked
    c.says("Medium is gone. Another size, a return, or leave it?", "choose_alternative")
    assert c.grade(must_offer).needed_clarification_asked
