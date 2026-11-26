"""Sessions, reads and proposals, against a real database and the v1 fixture."""

import uuid

import pytest
from sqlalchemy import text

from app.clock import FixedClock
from app.fixtures import reset
from app.policy import Action
from app.services.context import start_session
from app.services.errors import Refusal
from app.services.orders import check_options, list_orders
from app.services.proposals import confirm, decline, propose

R, X = Action.RETURN, Action.EXCHANGE


@pytest.fixture
def shop(db):
    fx = reset(db, "two_shirts_v1")
    clock = FixedClock(fx.today)

    def session(persona):
        return start_session(db, persona, "workflow", "test", clock)

    return session


def proposals(engine, ctx):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT version, status::text FROM proposals WHERE session_id = :s "
                 "ORDER BY version"),
            {"s": ctx.session_id},
        ).all()


def refused(code, fn, *args):
    with pytest.raises(Refusal) as e:
        fn(*args)
    assert e.value.code == code
    return e.value


# ---------------------------------------------------------------- sessions, reads

def test_unknown_persona_is_refused(shop, db):
    refused("not_found", start_session, db, "nobody", "workflow", "test", FixedClock(None))


def test_list_orders_shows_only_own_units(shop):
    units = list_orders(shop("amira"))
    assert {u.unit_id for u in units} == {"u_amira_blue", "u_amira_white"}


def test_other_shoppers_unit_is_not_found(shop):
    # PRD: asking for another persona's order returns nothing and changes nothing.
    refused("not_found", check_options, shop("amira"), "u_ben_chinos", R)


def test_day_30_eligible_day_31_expired(shop):
    ben = shop("ben")
    assert check_options(ben, "u_ben_chinos", R)[1].eligible
    assert check_options(ben, "u_ben_linen", R)[1].reason == "expired"


def test_check_options_reports_sizes_in_stock(shop):
    unit, d = check_options(shop("amira"), "u_amira_blue", X, "L")
    assert (unit.size, d.reason, d.sizes_in_stock) == ("S", "out_of_stock", ["M"])


# ---------------------------------------------------------------- proposals

def test_exchange_proposal_is_open_version_1(shop, db):
    amira = shop("amira")
    p = propose(amira, "u_amira_blue", X, "M")
    assert (p.version, p.status) == (1, "open")
    assert p.summary == "Exchange Oxford shirt (blue), size S, from order o_amira_1 for size M."
    assert proposals(db, amira) == [(1, "open")]


def test_return_proposal_has_no_size(shop):
    p = propose(shop("amira"), "u_amira_white", R, "M")   # size ignored for a return
    assert (p.action, p.replacement_size) == ("return", None)


def test_exchange_needs_a_size(shop):
    refused("size_required", propose, shop("amira"), "u_amira_blue", X)


def test_ineligible_request_creates_no_proposal(shop, db):
    amira = shop("amira")
    r = refused("out_of_stock", propose, amira, "u_amira_blue", X, "L")
    assert r.data["sizes_in_stock"] == ["M"]
    assert proposals(db, amira) == []


def test_final_sale_and_no_delivery_are_refused(shop):
    chloe = shop("chloe")
    refused("final_sale", propose, chloe, "u_chloe_jumper", R)
    refused("needs_review", propose, chloe, "u_chloe_jacket", R)


def test_cannot_propose_on_another_shoppers_unit(shop):
    refused("not_found", propose, shop("amira"), "u_ella_blue", X, "M")


# ---------------------------------------------------------------- corrections and confirmation

def test_correction_supersedes_previous_version(shop, db):
    ben = shop("ben")
    propose(ben, "u_ben_chinos", X, "30")
    p2 = propose(ben, "u_ben_chinos", X, "34")
    assert p2.version == 2
    assert proposals(db, ben) == [(1, "superseded"), (2, "open")]


def test_old_confirmation_cannot_confirm_after_a_correction(shop):
    # PRD: shopper changes medium to large; the old confirmation cannot commit medium.
    ben = shop("ben")
    p1 = propose(ben, "u_ben_chinos", X, "30")
    p2 = propose(ben, "u_ben_chinos", X, "34")
    refused("stale_proposal", confirm, ben, p1.id, 1)
    assert confirm(ben, p2.id, 2) == "confirmed"


def test_correction_after_confirming_needs_fresh_confirmation(shop, db):
    ben = shop("ben")
    p1 = propose(ben, "u_ben_chinos", X, "30")
    confirm(ben, p1.id, 1)
    propose(ben, "u_ben_chinos", X, "34")
    assert proposals(db, ben) == [(1, "superseded"), (2, "open")]


def test_confirm_with_wrong_version_is_stale(shop):
    amira = shop("amira")
    p = propose(amira, "u_amira_blue", X, "M")
    refused("stale_proposal", confirm, amira, p.id, 2)


def test_double_click_confirm_is_harmless(shop, db):
    amira = shop("amira")
    p = propose(amira, "u_amira_blue", X, "M")
    assert confirm(amira, p.id, 1) == confirm(amira, p.id, 1) == "confirmed"
    assert proposals(db, amira) == [(1, "confirmed")]


def test_cannot_confirm_another_sessions_proposal(shop):
    p = propose(shop("amira"), "u_amira_blue", X, "M")
    other = shop("amira")                    # same shopper, different session
    refused("not_found", confirm, other, p.id, 1)
    refused("not_found", confirm, other, uuid.uuid4(), 1)


def test_declined_proposal_cannot_be_confirmed(shop, db):
    amira = shop("amira")
    p = propose(amira, "u_amira_white", R)
    decline(amira, p.id)
    refused("stale_proposal", confirm, amira, p.id, 1)
    assert proposals(db, amira) == [(1, "declined")]
