"""The tool layer: schemas, results, refusals as data, and the tool_calls log."""

import json

import pytest
from sqlalchemy import text

from app import tools
from app.clock import FixedClock
from app.fixtures import read_fixture, reset
from app.services.context import start_session
from app.services.proposals import confirm
from app.tools import TOOLS, call_tool, tool_schemas

TODAY = read_fixture("two_shirts_v1").today


@pytest.fixture
def amira(db):
    fx = reset(db, "two_shirts_v1")
    return start_session(db, "amira", "agent", tools.TOOLSET_VERSION, FixedClock(fx.today))


def logged(engine, ctx):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT turn, tool, result_status, reason_code, duration_ms FROM tool_calls "
                 "WHERE session_id = :s ORDER BY id"),
            {"s": ctx.session_id},
        ).all()


def count(engine, table):
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()


# ---------------------------------------------------------------- schemas

def test_six_tools_and_no_confirm():
    assert set(TOOLS) == {"list_orders", "check_options", "propose", "commit_action",
                          "get_action_status", "request_review"}
    assert "confirm" not in TOOLS and "decline" not in TOOLS


def test_schemas_are_json_schema_without_a_shopper_argument():
    for s in tool_schemas():
        assert set(s) == {"name", "description", "input_schema"}
        assert s["input_schema"]["type"] == "object"
        assert "shopper_id" not in json.dumps(s)
        json.dumps(s)  # serialisable as sent to a model


def test_write_tools_are_marked():
    assert {n for n, t in TOOLS.items() if t.writes} == {"propose", "commit_action",
                                                         "request_review"}


# ---------------------------------------------------------------- results and logging

def test_list_orders_returns_json_and_is_logged(amira, db):
    r = call_tool(amira, "list_orders", {}, turn=1)
    assert r.status == "ok"
    ids = {u["order_unit_id"] for u in r.data["units"]}
    assert ids == {"u_amira_blue", "u_amira_white"}
    json.dumps(r.as_dict())  # dates are ISO strings, everything serialisable
    [(turn, tool, status, reason, ms)] = logged(db, amira)
    assert (turn, tool, status, reason) == (1, "list_orders", "ok", None)
    assert ms is not None and ms >= 0


def test_refusal_comes_back_as_data_and_is_logged(amira, db):
    r = call_tool(amira, "propose", {"order_unit_id": "u_amira_blue", "action": "exchange",
                                     "replacement_size": "L"}, turn=1)
    assert r.as_dict() == {"status": "refused", "reason": "out_of_stock",
                           "data": {"sizes_in_stock": ["M"], "existing_reference": None}}
    assert logged(db, amira)[0][2:4] == ("refused", "out_of_stock")
    assert count(db, "proposals") == 0


def test_invented_argument_is_rejected_and_nothing_written(amira, db):
    r = call_tool(amira, "propose", {"order_unit_id": "u_ben_chinos", "action": "return",
                                     "shopper_id": "sh_ben"}, turn=1)
    assert (r.status, r.reason) == ("error", "invalid_args")
    assert r.data["problems"][0]["field"] == "shopper_id"
    assert count(db, "proposals") == 0
    assert logged(db, amira)[0][2:4] == ("error", "invalid_args")


def test_bad_enum_value_is_rejected(amira):
    r = call_tool(amira, "check_options", {"order_unit_id": "u_amira_blue",
                                           "action": "refund"}, turn=1)
    assert (r.status, r.reason) == ("error", "invalid_args")


def test_unknown_tool_is_rejected_and_logged(amira, db):
    r = call_tool(amira, "confirm", {"proposal_id": "x"}, turn=1)
    assert (r.status, r.reason) == ("error", "unknown_tool")
    assert logged(db, amira)[0][1:4] == ("confirm", "error", "unknown_tool")


def test_commit_without_shopper_confirmation_is_an_invalid_write(amira, db):
    p = call_tool(amira, "propose", {"order_unit_id": "u_amira_blue", "action": "exchange",
                                     "replacement_size": "M"}, turn=1)
    r = call_tool(amira, "commit_action", {"proposal_id": p.data["proposal_id"]}, turn=1)
    assert (r.status, r.reason) == ("refused", "not_confirmed")
    assert count(db, "actions") == 0
    # The grader counts this row: a refused call to a write tool.
    assert logged(db, amira)[1][1:4] == ("commit_action", "refused", "not_confirmed")


def test_happy_path_through_tools(amira, db):
    p = call_tool(amira, "propose", {"order_unit_id": "u_amira_blue", "action": "exchange",
                                     "replacement_size": "M"}, turn=1)
    assert p.data["summary"].startswith("Exchange Oxford shirt (blue)")
    confirm(amira, p.data["proposal_id"], p.data["version"])   # the shopper's button
    c = call_tool(amira, "commit_action", {"proposal_id": p.data["proposal_id"]}, turn=2)
    assert c.status == "ok" and c.data["reference"].startswith("RX-")
    s = call_tool(amira, "get_action_status", {"order_unit_id": "u_amira_blue"}, turn=2)
    assert s.data["reference"] == c.data["reference"]
    assert [row[:3] for row in logged(db, amira)] == [
        (1, "propose", "ok"), (2, "commit_action", "ok"), (2, "get_action_status", "ok")]


def test_request_review_through_tools(amira):
    args = {"reason": "shopper_request", "question": "Can I talk to someone?"}
    first = call_tool(amira, "request_review", args, turn=1)
    again = call_tool(amira, "request_review", args, turn=2)
    assert first.data["existing_case"] is False and again.data["existing_case"] is True


def test_unexpected_error_hides_internals_but_is_logged(amira, db, monkeypatch, caplog):
    def boom(ctx, a):
        raise RuntimeError("connection string with a password in it")

    broken = tools.Tool("list_orders", "x", tools.NoArgs, boom, writes=False)
    monkeypatch.setitem(TOOLS, "list_orders", broken)
    r = call_tool(amira, "list_orders", {}, turn=1)
    assert r.as_dict() == {"status": "error", "reason": "internal_error",
                           "data": {"error_type": "RuntimeError"}}
    assert "password" not in json.dumps(r.as_dict())
    assert logged(db, amira)[0][2:4] == ("error", "internal_error")
    assert "connection string" in caplog.text   # developers still see the real error


# ---------------------------------------------------------------- tools-v2 (harness experiment)

def check(db, persona, unit, action, size=None):
    ctx = start_session(db, persona, "agent", "test", FixedClock(TODAY))
    args = {"order_unit_id": unit, "action": action} | ({"size": size} if size else {})
    return call_tool(ctx, "check_options", args, 1).data


def test_v1_check_options_has_no_next_step(db):
    reset(db, "two_shirts_v1")
    assert "next_step" not in check(db, "amira", "u_amira_blue", "exchange", "M")


@pytest.mark.parametrize("persona,unit,action,size,ask,review", [
    ("chloe", "u_chloe_jumper", "return", None, "offer_review", "final_sale_dispute"),
    ("chloe", "u_chloe_jacket", "return", None, "offer_review", "no_delivery_record"),
    ("ben", "u_ben_linen", "return", None, "offer_review", "policy_exception"),
    ("amira", "u_amira_blue", "exchange", "L", "choose_alternative", None),
    ("amira", "u_amira_blue", "exchange", "S", "choose_alternative", None),
    ("amira", "u_amira_blue", "exchange", None, "which_size", None),
    ("amira", "u_amira_blue", "exchange", "M", "confirm", None),
    ("ben", "u_ben_chinos", "return", None, "confirm", None),
])
def test_v2_next_step_names_the_question_and_review_reason(db, monkeypatch, persona, unit,
                                                          action, size, ask, review):
    reset(db, "two_shirts_v1")
    monkeypatch.setattr(tools, "TOOLSET_VERSION", "tools-v2")
    step = check(db, persona, unit, action, size)["next_step"]
    assert step["ask"] == ask and step.get("review_reason") == review
    if review:
        assert f"request_review with reason {review}" in step["do"]


def test_v2_existing_action_points_to_action_conflict(db, monkeypatch):
    reset(db, "two_shirts_v1")
    monkeypatch.setattr(tools, "TOOLSET_VERSION", "tools-v2")
    ctx = start_session(db, "amira", "agent", "test", FixedClock(TODAY))
    p = call_tool(ctx, "propose", {"order_unit_id": "u_amira_white", "action": "return"}, 1).data
    confirm(ctx, p["proposal_id"], p["version"])
    ref = call_tool(ctx, "commit_action", {"proposal_id": p["proposal_id"]}, 2).data["reference"]
    step = check(db, "amira", "u_amira_white", "exchange", "M")["next_step"]
    assert step["review_reason"] == "action_conflict" and ref in step["do"]


def test_unknown_toolset_is_rejected(monkeypatch):
    import importlib

    from app.config import settings
    monkeypatch.setattr(settings, "toolset", "tools-v9")
    with pytest.raises(ValueError, match="unknown toolset"):
        importlib.reload(tools)
    monkeypatch.setattr(settings, "toolset", "tools-v1")
    importlib.reload(tools)
