"""HTTP API: thin wrappers over services, refusals mapped to status codes."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.clock import FixedClock
from app.fixtures import read_fixture, reset
from app.policy import Action
from app.services.context import load_session
from app.services.proposals import propose

TODAY = read_fixture("two_shirts_v1").today


@pytest.fixture
def client(db):
    reset(db, "two_shirts_v1")
    return TestClient(create_app(db, FixedClock(TODAY), allow_reset=True))


def start(client, persona="amira"):
    r = client.post("/sessions", json={"persona": persona, "assistant": "workflow"})
    assert r.status_code == 201
    return r.json()["session_id"]


def offer(db, session_id, unit="u_amira_blue", size="M"):
    """Stand-in for an assistant: create a proposal in this session."""
    ctx = load_session(db, uuid.UUID(session_id), FixedClock(TODAY))
    return propose(ctx, unit, Action.EXCHANGE, size)


def test_health(client):
    assert client.get("/health").json() == {"ok": True}


def test_start_session_and_list_own_orders(client):
    sid = start(client)
    units = client.get(f"/sessions/{sid}/orders").json()["units"]
    assert {u["order_unit_id"] for u in units} == {"u_amira_blue", "u_amira_white"}


def test_unknown_persona_and_unknown_session_are_404(client):
    r = client.post("/sessions", json={"persona": "nobody", "assistant": "agent"})
    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    assert client.get(f"/sessions/{uuid.uuid4()}/orders").status_code == 404


def test_bad_assistant_name_is_422(client):
    r = client.post("/sessions", json={"persona": "amira", "assistant": "gpt"})
    assert r.status_code == 422


def test_confirm_card_shows_the_live_proposal(client, db):
    sid = start(client)
    assert client.get(f"/sessions/{sid}/proposal").json() == {"proposal": None}
    p = offer(db, sid)
    card = client.get(f"/sessions/{sid}/proposal").json()["proposal"]
    assert card["proposal_id"] == str(p.id)
    assert (card["version"], card["status"], card["replacement_size"]) == (1, "open", "M")
    assert card["summary"] == p.summary


def test_confirm_button(client, db):
    sid = start(client)
    p = offer(db, sid)
    url = f"/sessions/{sid}/proposals/{p.id}/confirm"
    assert client.post(url, json={"version": 1}).json() == {"status": "confirmed"}
    assert client.post(url, json={"version": 1}).json() == {"status": "confirmed"}  # double click
    assert client.get(f"/sessions/{sid}/proposal").json()["proposal"]["status"] == "confirmed"


def test_stale_confirm_is_409_with_code(client, db):
    sid = start(client)
    p1 = offer(db, sid, size="M")
    offer(db, sid, unit="u_amira_white", size="M")      # supersedes p1
    r = client.post(f"/sessions/{sid}/proposals/{p1.id}/confirm", json={"version": 1})
    assert (r.status_code, r.json()["code"]) == (409, "stale_proposal")


def test_cannot_confirm_from_another_session(client, db):
    p = offer(db, start(client))
    other = start(client)
    r = client.post(f"/sessions/{other}/proposals/{p.id}/confirm", json={"version": 1})
    assert r.status_code == 404


def test_decline_button(client, db):
    sid = start(client)
    p = offer(db, sid)
    r = client.post(f"/sessions/{sid}/proposals/{p.id}/decline")
    assert r.json() == {"status": "declined"}
    assert client.get(f"/sessions/{sid}/proposal").json() == {"proposal": None}


def test_reset_reloads_the_shop(client):
    sid = start(client)
    r = client.post("/reset")
    assert r.json() == {"fixture": "two_shirts_v1", "today": "2026-12-01"}
    assert client.get(f"/sessions/{sid}/orders").status_code == 404   # sessions wiped


def test_reset_is_off_by_default(db):
    locked = TestClient(create_app(db, FixedClock(TODAY)))
    assert locked.post("/reset").status_code == 403
