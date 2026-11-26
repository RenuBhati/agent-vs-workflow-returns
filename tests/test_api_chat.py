"""Chat over HTTP: messages go to the session's assistant; only the button commits."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import create_app
from app.assistants.workflow import WorkflowAssistant
from app.clock import FixedClock
from app.fixtures import read_fixture, reset
from app.llm import FakeLLM

TODAY = read_fixture("two_shirts_v1").today


@pytest.fixture
def chat(db):
    reset(db, "two_shirts_v1")

    def make(*replies):
        llm = FakeLLM(list(replies))
        client = TestClient(create_app(db, FixedClock(TODAY),
                                       assistants={"workflow": lambda: WorkflowAssistant(llm)}))
        sid = client.post("/sessions", json={"persona": "amira",
                                             "assistant": "workflow"}).json()["session_id"]
        return client, sid
    return make


def say(client, sid, msg):
    r = client.post(f"/sessions/{sid}/messages", json={"text": msg})
    assert r.status_code == 200, r.text
    return r.json()


def q(db, sql, **params):
    with db.connect() as conn:
        return conn.execute(text(sql), params).all()


def test_full_conversation_over_http(chat, db):
    client, sid = chat({"intent": "exchange", "items": [1], "size": "M"})
    turn = say(client, sid, "Exchange the blue one for a medium")
    assert turn["question_kind"] == "confirm"
    p = turn["proposal"]
    assert (p["order_unit_id"], p["action"], p["replacement_size"]) == ("u_amira_blue",
                                                                        "exchange", "M")
    done = client.post(f"/sessions/{sid}/proposals/{p['proposal_id']}/confirm",
                       json={"version": p["version"]}).json()
    assert done["status"] == "confirmed" and "RX-" in done["reply"] and done["proposal"] is None
    roles = [m["role"] for m in client.get(f"/sessions/{sid}/messages").json()["messages"]]
    assert roles == ["shopper", "assistant", "assistant"]


def test_typing_yes_does_not_commit(chat, db):
    client, sid = chat({"intent": "exchange", "items": [1], "size": "M"}, {"yes": True})
    say(client, sid, "Exchange the blue one for a medium")
    turn = say(client, sid, "yes, do it")
    assert "Confirm button" in turn["reply"] and turn["proposal"]["status"] == "open"
    assert q(db, "SELECT * FROM actions") == []
    assert len(q(db, "SELECT * FROM proposals")) == 1     # not re-proposed either


def test_turn_numbers_line_up_across_tools_and_model_calls(chat, db):
    client, sid = chat({"intent": "exchange", "size": "M"}, {"items": [1]})
    say(client, sid, "Exchange my shirt for a medium")    # turn 1: asks which item
    p = say(client, sid, "the blue one")["proposal"]        # turn 2: proposes
    client.post(f"/sessions/{sid}/proposals/{p['proposal_id']}/confirm", json={"version": 1})
    tools = q(db, "SELECT turn, tool FROM tool_calls ORDER BY id")
    assert [(t.turn, t.tool) for t in tools] == [
        (1, "list_orders"), (2, "check_options"), (2, "propose"), (3, "commit_action")]
    assert [m.turn for m in q(db, "SELECT turn FROM model_calls ORDER BY id")] == [1, 2]


def test_session_records_the_full_config(chat, db):
    _, sid = chat()
    version = q(db, "SELECT config_version FROM sessions WHERE id = :s", s=sid)[0][0]
    assert version == "workflow-v1+tools-v1+fake@test"


def test_assistant_not_built_yet_is_501(chat):
    client, _ = chat()
    sid = client.post("/sessions", json={"persona": "amira",
                                         "assistant": "agent"}).json()["session_id"]
    assert client.post(f"/sessions/{sid}/messages", json={"text": "hi"}).status_code == 501
