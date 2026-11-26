"""The agent loop, driven by a scripted fake model.

Each scripted reply is one model step: tool calls, or a reply to the shopper.
The tests check the loop's plumbing, not the model's judgement.
"""

import json

import pytest
from sqlalchemy import text

from app.assistants.agent import MAX_STEPS, AgentAssistant
from app.clock import FixedClock
from app.fixtures import reset
from app.llm import FakeLLM, LLMReply
from app.services.context import start_session
from app.services.proposals import confirm, live_proposal


def call(name, **args):
    return LLMReply(tool_calls=[{"name": name, "arguments": args}])


def respond(text_, kind="none"):
    return call("respond_to_shopper", text=text_, question_kind=kind)


@pytest.fixture
def session(db):
    fx = reset(db, "two_shirts_v1")

    def start(replies, persona="amira"):
        ctx = start_session(db, persona, "agent", "test", FixedClock(fx.today))
        llm = FakeLLM(replies)
        return ctx, AgentAssistant(llm), llm
    return start


def rows(db, sql):
    with db.connect() as conn:
        return conn.execute(text(sql)).all()


def test_happy_path_tools_then_confirm_then_commit(session, db):
    ctx, agent, llm = session([
        call("list_orders"),
        call("check_options", order_unit_id="u_amira_blue", action="exchange", size="M"),
        call("propose", order_unit_id="u_amira_blue", action="exchange", replacement_size="M"),
        respond("Exchange the blue shirt for M. Press Confirm.", "confirm"),
    ])
    r = agent.handle(ctx, "Exchange the blue one for a medium", 1)
    assert r.kind == "confirm"
    p = live_proposal(ctx)
    confirm(ctx, p.id, p.version)

    llm.replies += [call("commit_action", proposal_id=str(p.id)),
                    respond("Done. Your reference is in the tool result.", "none")]
    done = agent.on_confirm(ctx, str(p.id), 2)
    assert done.kind is None
    assert len(rows(db, "SELECT * FROM actions")) == 1
    # The model saw each tool result, and the Confirm event, in its history.
    last = llm.calls[-1]["messages"]
    assert any(m["role"] == "tool" and m["tool_name"] == "commit_action" for m in last)
    assert any("pressed Confirm" in m.get("content", "") for m in last)


def test_tools_and_reply_tool_are_offered_and_only_business_tools_are_logged(session, db):
    ctx, agent, llm = session([call("list_orders"), respond("Which shirt?", "which_item")])
    assert agent.handle(ctx, "Exchange my shirt", 1).kind == "which_item"
    names = [t["name"] for t in llm.calls[0]["tools"]]
    assert "respond_to_shopper" in names and "propose" in names and "confirm" not in names
    assert [r.tool for r in rows(db, "SELECT tool FROM tool_calls")] == ["list_orders"]


def test_history_carries_over_between_turns(session):
    ctx, agent, llm = session([respond("Which shirt?", "which_item"),
                               respond("Thanks.", "none")])
    agent.handle(ctx, "Exchange my shirt", 1)
    agent.handle(ctx, "the white one", 2)
    contents = [m.get("content") for m in llm.calls[1]["messages"]]
    assert "Exchange my shirt" in contents and "the white one" in contents
    assert llm.calls[1]["messages"][0]["role"] == "system"


def test_refused_tool_result_goes_back_to_the_model(session, db):
    ctx, agent, llm = session([
        call("propose", order_unit_id="u_amira_blue", action="exchange", replacement_size="L"),
        respond("L is out of stock. Another size, a return, or leave it?",
                "choose_alternative")])
    assert agent.handle(ctx, "blue one in large", 1).kind == "choose_alternative"
    tool_msg = json.loads(llm.calls[1]["messages"][-1]["content"])
    assert (tool_msg["status"], tool_msg["reason"]) == ("refused", "out_of_stock")


def test_step_limit_offers_a_person(session):
    ctx, agent, _ = session([call("list_orders") for _ in range(MAX_STEPS)])
    r = agent.handle(ctx, "help", 1)
    assert r.kind == "offer_review" and "person" in r.text


def test_model_errors_are_logged_and_retried_within_the_limit(session, db):
    ctx, agent, _ = session([LLMReply(error="malformed_tool_call"), respond("Hi!", "none")])
    assert agent.handle(ctx, "hello", 1).text == "Hi!"
    assert [r.error for r in rows(db, "SELECT error FROM model_calls ORDER BY id")] == [
        "malformed_tool_call", None]


def test_prose_without_the_reply_tool_is_still_shown(session):
    ctx, agent, _ = session([LLMReply(content="Which shirt do you mean?")])
    r = agent.handle(ctx, "Exchange my shirt", 1)
    assert r.text == "Which shirt do you mean?" and r.kind == "other"


def test_unknown_question_kind_is_treated_as_no_question(session):
    ctx, agent, _ = session([respond("Done.", "made_up_kind")])
    assert agent.handle(ctx, "thanks", 1).kind is None


def test_prose_reply_with_a_waiting_proposal_asks_for_confirm(session):
    ctx, agent, _ = session([
        call("propose", order_unit_id="u_amira_blue", action="exchange", replacement_size="M"),
        LLMReply(content="I've set up the exchange. Please press the Confirm button.")])
    assert agent.handle(ctx, "Exchange the blue one for a medium", 1).kind == "confirm"


class SeedSpy(FakeLLM):
    def chat(self, messages, tools=None, schema=None, seed_offset=0):
        self.offsets = getattr(self, "offsets", []) + [seed_offset]
        return super().chat(messages, tools, schema)


def test_each_step_uses_a_different_seed(db):
    from app.clock import FixedClock as FC
    fx = reset(db, "two_shirts_v1")
    ctx = start_session(db, "amira", "agent", "test", FC(fx.today))
    llm = SeedSpy([LLMReply(error="malformed_tool_call"), respond("Hi!", "none")])
    AgentAssistant(llm).handle(ctx, "hello", 1)
    assert llm.offsets == [0, 2]
