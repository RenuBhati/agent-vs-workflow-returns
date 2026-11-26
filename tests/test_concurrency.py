"""Races, with real threads and separate database connections.

Each round resets the shop, prepares confirmed proposals, then releases two
threads at the same instant with a barrier. Timing varies between rounds, so
some rounds overlap fully (the lock or index decides) and some don't (the
ordinary recheck decides). The invariants must hold either way.
"""

import threading
from collections import Counter
from functools import partial

import pytest
from sqlalchemy import text

from app.clock import FixedClock
from app.fixtures import reset
from app.policy import Action
from app.services.actions import commit_action
from app.services.context import start_session
from app.services.errors import Refusal
from app.services.proposals import confirm, propose

ROUNDS = 25
X = Action.EXCHANGE


def race(*calls):
    """Run each call in its own thread, all starting together. Returns results or Refusals."""
    barrier = threading.Barrier(len(calls))
    results = [None] * len(calls)

    def run(i, fn):
        barrier.wait()
        try:
            results[i] = fn()
        except Refusal as r:
            results[i] = r
        except Exception as e:  # noqa: BLE001 - any other error is a bug; keep it so the test fails loudly
            results[i] = e

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def one(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


@pytest.fixture
def fresh(db):
    def setup():
        fx = reset(db, "two_shirts_v1")
        clock = FixedClock(fx.today)
        return lambda persona: start_session(db, persona, "workflow", "test", clock)
    return setup


def confirmed(ctx, unit, size):
    p = propose(ctx, unit, X, size)
    confirm(ctx, p.id, p.version)
    return p


def test_two_shoppers_race_for_the_last_unit(fresh, db):
    # PRD: two shoppers compete for the last unit → at most one exchange succeeds.
    paths = Counter()
    for _ in range(ROUNDS):
        session = fresh()
        amira, ella = session("amira"), session("ella")
        pa = confirmed(amira, "u_amira_blue", "M")
        pe = confirmed(ella, "u_ella_blue", "M")

        results = race(partial(commit_action, amira, pa.id), partial(commit_action, ella, pe.id))

        wins = [r for r in results if not isinstance(r, Exception)]
        losses = [r for r in results if isinstance(r, Refusal)]
        assert len(wins) == 1 and len(losses) == 1, results
        assert losses[0].code == "out_of_stock"
        paths[losses[0].message or "recheck saw no stock"] += 1
        assert one(db, "SELECT stock FROM skus WHERE id = 'oxford_blue_M'") == 0
        assert one(db, "SELECT count(*) FROM actions") == 1
    print(f"\nlast-unit loser was caught by: {dict(paths)}")


def test_same_proposal_committed_twice_at_once(fresh, db):
    # PRD: confirmation sent twice concurrently → same reference, one entry, one decrement.
    for _ in range(ROUNDS):
        amira = fresh()("amira")
        p = confirmed(amira, "u_amira_blue", "M")

        a, b = race(partial(commit_action, amira, p.id), partial(commit_action, amira, p.id))

        assert not isinstance(a, Exception) and not isinstance(b, Exception), (a, b)
        assert a.reference == b.reference
        assert sorted([a.repeated, b.repeated]) == [False, True]   # one did it, one found it
        assert one(db, "SELECT count(*) FROM actions") == 1
        assert one(db, "SELECT stock FROM skus WHERE id = 'oxford_blue_M'") == 0


def test_two_sessions_race_on_the_same_unit(fresh, db):
    # Same shopper in two tabs: exchange the chinos for 30 in one, 34 in the other.
    paths = Counter()
    for _ in range(ROUNDS):
        session = fresh()
        tab1, tab2 = session("ben"), session("ben")
        p1 = confirmed(tab1, "u_ben_chinos", "30")
        p2 = confirmed(tab2, "u_ben_chinos", "34")

        results = race(partial(commit_action, tab1, p1.id), partial(commit_action, tab2, p2.id))

        wins = [r for r in results if not isinstance(r, Exception)]
        losses = [r for r in results if isinstance(r, Refusal)]
        assert len(wins) == 1 and len(losses) == 1, results
        assert losses[0].code == "already_actioned"
        paths[losses[0].message or "recheck saw the action"] += 1
        assert one(db, "SELECT count(*) FROM actions WHERE order_unit_id = 'u_ben_chinos'") == 1
        # Only the winner's size lost a unit; the loser's decrement was rolled back.
        won_size = "30" if wins[0].replacement_sku_id == "chinos_navy_30" else "34"
        lost_size = "34" if won_size == "30" else "30"
        assert one(db, f"SELECT stock FROM skus WHERE id = 'chinos_navy_{won_size}'") == 1
        assert one(db, f"SELECT stock FROM skus WHERE id = 'chinos_navy_{lost_size}'") == 2
    print(f"\nsame-unit loser was caught by: {dict(paths)}")
