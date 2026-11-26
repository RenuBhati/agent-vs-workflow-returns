"""Commit, action status and review, against a real database and the v1 fixture.

Each test names the PRD acceptance rule it proves. Concurrent versions of the
retry and last-unit cases are in tests/test_concurrency.py.
"""

import dataclasses
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.clock import FixedClock
from app.fixtures import reset
from app.policy import Action
from app.services.actions import commit_action, get_action_status
from app.services.context import start_session
from app.services.errors import Refusal
from app.services.proposals import confirm, decline, propose
from app.services.review import ReviewReason, request_review

R, X = Action.RETURN, Action.EXCHANGE


@pytest.fixture
def shop(db):
    fx = reset(db, "two_shirts_v1")

    def session(persona):
        return start_session(db, persona, "workflow", "test", FixedClock(fx.today))

    return session


def q(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).all()


def stock(engine, sku):
    return q(engine, "SELECT stock FROM skus WHERE id = :k", k=sku)[0][0]


def refused(code, fn, *args):
    with pytest.raises(Refusal) as e:
        fn(*args)
    assert e.value.code == code
    return e.value


def confirmed(ctx, unit, action, size=None):
    p = propose(ctx, unit, action, size)
    confirm(ctx, p.id, p.version)
    return p


# ---------------------------------------------------------------- commit

def test_confirmed_exchange_commits_once(shop, db):
    # PRD: correct unit has one exchange, medium stock falls by one, reference returned.
    amira = shop("amira")
    p = confirmed(amira, "u_amira_blue", X, "M")
    c = commit_action(amira, p.id)
    assert c.reference.startswith("RX-") and not c.repeated
    assert q(db, "SELECT order_unit_id, action::text, replacement_sku_id, reference "
                 "FROM actions") == [("u_amira_blue", "exchange", "oxford_blue_M", c.reference)]
    assert stock(db, "oxford_blue_M") == 0
    assert q(db, "SELECT status::text FROM proposals") == [("committed",)]


def test_return_commits_without_touching_stock(shop, db):
    amira = shop("amira")
    c = commit_action(amira, confirmed(amira, "u_amira_white", R).id)
    assert c.action == "return"
    assert stock(db, "oxford_white_S") == 2


def test_unconfirmed_proposal_cannot_commit(shop, db):
    amira = shop("amira")
    p = propose(amira, "u_amira_blue", X, "M")
    refused("not_confirmed", commit_action, amira, p.id)
    assert q(db, "SELECT count(*) FROM actions") == [(0,)]
    assert stock(db, "oxford_blue_M") == 1


def test_retry_after_lost_response_returns_same_reference(shop, db):
    # PRD: confirmation sent twice → same reference, one ledger entry, one stock decrement.
    amira = shop("amira")
    p = confirmed(amira, "u_amira_blue", X, "M")
    first = commit_action(amira, p.id)
    again = commit_action(amira, p.id)
    assert again.reference == first.reference and again.repeated
    assert q(db, "SELECT count(*) FROM actions") == [(1,)]
    assert stock(db, "oxford_blue_M") == 0


def test_stock_gone_between_proposal_and_commit(shop, db):
    # PRD: medium goes out of stock between proposal and confirmation → no exchange,
    # no decrement, explanation and alternatives.
    amira, ella = shop("amira"), shop("ella")
    pa = confirmed(amira, "u_amira_blue", X, "M")
    commit_action(ella, confirmed(ella, "u_ella_blue", X, "M").id)   # Ella takes the last M
    r = refused("out_of_stock", commit_action, amira, pa.id)
    assert r.data["sizes_in_stock"] == []                            # no other blue size left
    assert q(db, "SELECT order_unit_id FROM actions") == [("u_ella_blue",)]
    assert stock(db, "oxford_blue_M") == 0
    assert q(db, "SELECT status::text FROM proposals WHERE id = :p", p=pa.id) == [("expired",)]


def test_window_closes_between_proposal_and_commit(shop, db):
    # Proposed on day 30 (eligible); committed after midnight on day 31.
    ben = shop("ben")
    p = confirmed(ben, "u_ben_chinos", X, "34")
    tomorrow = dataclasses.replace(ben, clock=FixedClock(ben.clock.today() + timedelta(days=1)))
    refused("expired", commit_action, tomorrow, p.id)
    assert q(db, "SELECT count(*) FROM actions") == [(0,)]
    assert stock(db, "chinos_navy_34") == 2


def test_superseded_or_declined_proposal_cannot_commit(shop):
    ben = shop("ben")
    p1 = confirmed(ben, "u_ben_chinos", X, "30")
    propose(ben, "u_ben_chinos", X, "34")                 # correction
    refused("stale_proposal", commit_action, ben, p1.id)
    amira = shop("amira")
    p = propose(amira, "u_amira_white", R)
    decline(amira, p.id)
    refused("stale_proposal", commit_action, amira, p.id)


def test_decline_then_new_confirmed_return_creates_exactly_one(shop, db):
    # PRD: shopper declines → no request; a new confirmed proposal → exactly one return.
    amira = shop("amira")
    decline(amira, propose(amira, "u_amira_white", R).id)
    assert q(db, "SELECT count(*) FROM actions") == [(0,)]
    commit_action(amira, confirmed(amira, "u_amira_white", R).id)
    assert q(db, "SELECT action::text FROM actions") == [("return",)]


def test_second_action_on_same_unit_surfaces_the_first(shop):
    # PRD: a conflicting request surfaces the existing action.
    amira = shop("amira")
    c = commit_action(amira, confirmed(amira, "u_amira_white", R).id)
    later = shop("amira")                                  # a new conversation
    r = refused("already_actioned", propose, later, "u_amira_white", X, "M")
    assert r.data["existing_reference"] == c.reference


def test_cannot_commit_another_sessions_proposal(shop):
    p = confirmed(shop("amira"), "u_amira_blue", X, "M")
    refused("not_found", commit_action, shop("amira"), p.id)


# ---------------------------------------------------------------- action status

def test_action_status_before_and_after_commit(shop):
    amira = shop("amira")
    assert get_action_status(amira, "u_amira_blue").reference is None
    c = commit_action(amira, confirmed(amira, "u_amira_blue", X, "M").id)
    s = get_action_status(amira, "u_amira_blue")
    assert (s.action, s.reference) == ("exchange", c.reference)


def test_action_status_respects_ownership(shop):
    refused("not_found", get_action_status, shop("amira"), "u_ben_chinos")


# ---------------------------------------------------------------- review

def test_final_sale_dispute_creates_a_case_and_no_action(shop, db):
    # PRD: final sale, shopper disputes → policy explained, review creates a case,
    # no order action.
    chloe = shop("chloe")
    refused("final_sale", propose, chloe, "u_chloe_jumper", R)
    case = request_review(chloe, ReviewReason.FINAL_SALE_DISPUTE,
                          "Says the tag did not mark it final sale", "u_chloe_jumper")
    assert not case.reused
    assert q(db, "SELECT reason, order_unit_id, status::text FROM review_cases") == [
        ("final_sale_dispute", "u_chloe_jumper", "open")]
    assert q(db, "SELECT count(*) FROM actions") == [(0,)]
    assert stock(db, "jumper_grey_M") == 2


def test_repeated_review_request_reuses_the_case(shop, db):
    # PRD: repeated handoff requests for the same unresolved issue reuse the case.
    chloe = shop("chloe")
    first = request_review(chloe, ReviewReason.NO_DELIVERY_RECORD, "Where is it?",
                           "u_chloe_jacket")
    again = request_review(shop("chloe"), ReviewReason.NO_DELIVERY_RECORD, "Still waiting",
                           "u_chloe_jacket")
    assert again.id == first.id and again.reused
    assert q(db, "SELECT count(*) FROM review_cases") == [(1,)]


def test_review_without_a_unit_is_also_reused(shop):
    chloe = shop("chloe")
    first = request_review(chloe, ReviewReason.SHOPPER_REQUEST, "Can I talk to a person?")
    again = request_review(chloe, ReviewReason.SHOPPER_REQUEST, "Hello?")
    assert again.id == first.id


def test_different_reason_opens_a_separate_case(shop):
    chloe = shop("chloe")
    a = request_review(chloe, ReviewReason.FINAL_SALE_DISPUTE, "x", "u_chloe_jumper")
    b = request_review(chloe, ReviewReason.POLICY_EXCEPTION, "y", "u_chloe_jumper")
    assert a.id != b.id


def test_review_on_another_shoppers_unit_is_refused(shop, db):
    refused("not_found", request_review, shop("chloe"), ReviewReason.SHOPPER_REQUEST,
            "x", "u_amira_blue")
    assert q(db, "SELECT count(*) FROM review_cases") == [(0,)]
