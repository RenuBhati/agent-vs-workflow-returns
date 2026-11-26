"""The fixed workflow, driven by a scripted fake model (no Ollama needed).

Each scripted reply is what the extraction call would return for that message.
The tests check the state machine: which question it asks, what it proposes,
and what ends up in the database.
"""

import pytest
from sqlalchemy import text

from app.assistants.workflow import WorkflowAssistant
from app.clock import FixedClock
from app.fixtures import reset
from app.llm import FakeLLM, LLMReply
from app.services.context import start_session
from app.services.proposals import confirm, live_proposal


@pytest.fixture
def session(db):
    fx = reset(db, "two_shirts_v1")

    def start(persona, replies):
        ctx = start_session(db, persona, "workflow", "test", FixedClock(fx.today))
        llm = FakeLLM(replies)
        return ctx, WorkflowAssistant(llm), llm
    return start


def rows(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).all()


def press_confirm(ctx, wf, turn):
    p = live_proposal(ctx)
    confirm(ctx, p.id, p.version)
    return wf.on_confirm(ctx, str(p.id), turn)


def test_happy_path_exchange_blue_for_medium(session, db):
    ctx, wf, llm = session("amira", [{"intent": "exchange", "items": [1], "size": "M"}])
    r = wf.handle(ctx, "Exchange the blue one for a medium", 1)
    assert r.kind == "confirm" and "size M" in r.text and "blue" in r.text
    assert "Oxford" in llm.calls[0]["messages"][1]["content"]   # the model saw the items
    assert llm.calls[0]["schema"] is not None                   # structured output, no tools
    done = press_confirm(ctx, wf, 2)
    assert done.kind is None and "RX-" in done.text
    assert rows(db, "SELECT stock FROM skus WHERE id = 'oxford_blue_M'")[0].stock == 0


def test_ambiguous_item_asks_which_item_then_continues(session):
    ctx, wf, _ = session("amira", [{"intent": "exchange", "size": "M"}, {"items": [1]}])
    assert wf.handle(ctx, "Exchange my shirt for a medium", 1).kind == "which_item"
    assert wf.handle(ctx, "the blue one", 2).kind == "confirm"


def test_missing_size_asks_which_size_listing_stock(session):
    ctx, wf, _ = session("amira", [{"intent": "exchange", "items": [1]}, {"size": "M"}])
    r = wf.handle(ctx, "I'd like to exchange the blue shirt", 1)
    assert r.kind == "which_size" and "M" in r.text
    assert wf.handle(ctx, "medium please", 2).kind == "confirm"


def test_missing_action_asks_return_or_exchange(session):
    ctx, wf, _ = session("amira", [{"items": [2]}, {"intent": "return"}])
    assert wf.handle(ctx, "It's about the white shirt", 1).kind == "which_action"
    r = wf.handle(ctx, "return it", 2)
    assert r.kind == "confirm" and r.text.startswith("Return")


def test_out_of_stock_offers_alternatives_and_never_switches_silently(session, db):
    ctx, wf, _ = session("amira", [{"intent": "exchange", "items": [1], "size": "L"},
                                   {"intent": "return"}])
    r = wf.handle(ctx, "Exchange the blue one for a large", 1)
    assert r.kind == "choose_alternative" and "out of stock" in r.text
    assert live_proposal(ctx) is None             # nothing proposed before the shopper chose
    r = wf.handle(ctx, "just return it then", 2)
    assert r.kind == "confirm" and r.text.startswith("Return")


def test_identical_units_are_one_item_and_never_asked_about(session):
    ctx, wf, llm = session("dev", [{"intent": "return"}])
    r = wf.handle(ctx, "I want to return my t-shirt", 1)
    assert r.kind == "confirm"
    assert "2." not in llm.calls[0]["messages"][1]["content"]   # one numbered item


@pytest.mark.parametrize(("persona", "item", "reason", "review_reason"), [
    ("chloe", 2, "final sale", "final_sale_dispute"),   # newest order first: jacket is 1
    ("ben", 2, "30-day window", "policy_exception"),    # linen expired yesterday
])
def test_ineligible_explains_offers_review_and_files_it(session, db, persona, item, reason,
                                                        review_reason):
    ctx, wf, _ = session(persona, [{"intent": "return", "items": [item]}, {"yes": True}])
    words = {"chloe": "the jumper", "ben": "the linen shirt"}[persona]
    r = wf.handle(ctx, f"I want to return {words}", 1)
    assert r.kind == "offer_review" and reason in r.text
    r = wf.handle(ctx, "yes please", 2)
    assert r.kind is None and "person" in r.text
    assert [c.reason for c in rows(db, "SELECT reason::text FROM review_cases")] == [
        review_reason]


def test_declining_the_review_offer_changes_nothing(session, db):
    ctx, wf, _ = session("chloe", [{"intent": "return", "items": [2]}, {"yes": False}])
    wf.handle(ctx, "return the jumper", 1)
    assert wf.handle(ctx, "no thanks", 2).kind is None
    assert rows(db, "SELECT * FROM review_cases") == []


def test_asking_for_a_person_files_shopper_request(session, db):
    ctx, wf, _ = session("amira", [{"wants_person": True}])
    assert wf.handle(ctx, "can I talk to a human", 1).kind is None
    assert rows(db, "SELECT reason::text FROM review_cases")[0].reason == "shopper_request"


def test_correction_before_confirm_supersedes_the_proposal(session, db):
    ctx, wf, _ = session("amira", [{"intent": "exchange", "items": [1], "size": "M"},
                                   {"intent": "return"}])
    wf.handle(ctx, "Exchange the blue one for a medium", 1)
    r = wf.handle(ctx, "actually, just return it", 2)
    assert r.text.startswith("Return")
    assert [p.status for p in rows(db, "SELECT status::text FROM proposals ORDER BY version")
            ] == ["superseded", "open"]


def test_model_failure_apologises_and_logs_without_crashing(session, db):
    ctx, wf, _ = session("amira", [LLMReply(error="timeout"), "not json at all"])
    assert "didn't catch" in wf.handle(ctx, "Exchange the blue one", 1).text
    assert "didn't catch" in wf.handle(ctx, "Exchange the blue one", 2).text
    errors = [c.error for c in rows(db, "SELECT error FROM model_calls ORDER BY id")]
    assert errors == ["timeout", "invalid_extraction"]


def test_lost_race_at_commit_offers_alternatives(session, db):
    ctx, wf, _ = session("amira", [{"intent": "exchange", "items": [1], "size": "M"}])
    wf.handle(ctx, "Exchange the blue one for a medium", 1)
    with db.begin() as conn:   # someone else buys the last blue M
        conn.execute(text("UPDATE skus SET stock = 0 WHERE id = 'oxford_blue_M'"))
    r = press_confirm(ctx, wf, 2)
    assert r.kind == "choose_alternative" and "RX-" not in r.text
    assert rows(db, "SELECT * FROM actions") == []


def test_every_step_goes_through_the_logged_tool_path(session, db):
    ctx, wf, _ = session("amira", [{"intent": "exchange", "items": [1], "size": "M"}])
    wf.handle(ctx, "Exchange the blue one for a medium", 1)
    press_confirm(ctx, wf, 2)
    tools = [r.tool for r in rows(db, "SELECT tool FROM tool_calls ORDER BY id")]
    assert tools == ["list_orders", "check_options", "propose", "commit_action"]
    assert len(rows(db, "SELECT * FROM model_calls")) == 1   # one model call per message


def test_a_guessed_item_is_not_accepted_when_the_words_fit_two(session):
    # "my shirt" fits both of Amira's shirts; the model's pick of item 1 is a guess.
    ctx, wf, _ = session("amira", [{"intent": "exchange", "items": [1], "size": "M"},
                                   {"items": [2]}])
    assert wf.handle(ctx, "Exchange my shirt for a medium", 1).kind == "which_item"
    r = wf.handle(ctx, "the white one", 2)
    assert r.kind == "confirm" and "white" in r.text


def test_no_with_a_correction_is_not_a_cancellation(session):
    ctx, wf, _ = session("amira", [{"intent": "exchange", "items": [1], "size": "M"},
                                   {"yes": False, "items": [2], "size": "M"}])
    wf.handle(ctx, "Exchange the blue one for a medium", 1)
    r = wf.handle(ctx, "No, I meant the white one", 2)
    assert r.kind == "confirm" and "white" in r.text


def test_declining_review_never_files_even_if_misread_as_review(session, db):
    ctx, wf, _ = session("ben", [{"intent": "return", "items": [2]},
                                 {"yes": False, "intent": "review"}])
    wf.handle(ctx, "return the linen shirt", 1)
    assert wf.handle(ctx, "No, thank you.", 2).kind is None
    assert rows(db, "SELECT * FROM review_cases") == []


def test_dispute_in_the_first_message_files_the_specific_reason(session, db):
    ctx, wf, _ = session("chloe", [{"intent": "review", "items": [2]}])
    wf.handle(ctx, "I dispute the final sale on my grey jumper", 1)
    assert rows(db, "SELECT reason::text FROM review_cases")[0].reason == "final_sale_dispute"


def test_off_topic_gets_a_redirect_not_a_loop(session):
    ctx, wf, _ = session("amira", [{"yes": False}])   # yes/no filled in with nothing asked
    r = wf.handle(ctx, "What's the weather in London?", 1)
    assert r.kind is None and "return" in r.text
