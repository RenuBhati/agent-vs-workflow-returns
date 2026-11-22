"""Each test tries to break one schema constraint and expects Postgres to refuse.

These guarantees hold whatever the application code or the model does.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


def run(engine, sql, **params):
    with engine.begin() as conn:
        return conn.execute(text(sql), params)


@pytest.fixture
def shop(db):
    """One shopper with two units of a shirt; one medium left in stock."""
    run(db, """
        INSERT INTO shoppers VALUES ('sh_1', 'Amira', 'amira');
        INSERT INTO styles  VALUES ('oxford_blue', 'Oxford shirt', 'blue');
        INSERT INTO skus    VALUES ('oxford_blue_S', 'oxford_blue', 'S', 0),
                                   ('oxford_blue_M', 'oxford_blue', 'M', 1);
        INSERT INTO orders  VALUES ('o_1', 'sh_1', '2026-11-01', '2026-11-05');
        INSERT INTO order_units VALUES ('u_1', 'o_1', 'oxford_blue_S', false),
                                       ('u_2', 'o_1', 'oxford_blue_S', false);
    """)
    session_id = run(db, """
        INSERT INTO sessions (shopper_id, assistant, config_version, trace_id)
        VALUES ('sh_1', 'workflow', 'test', 't_1') RETURNING id
    """).scalar()
    return db, session_id


def proposal(engine, session_id, version, unit="u_1", status="committed"):
    return run(engine, """
        INSERT INTO proposals (session_id, order_unit_id, action, replacement_sku_id,
                               version, status, summary, confirmed_at)
        VALUES (:s, :u, 'exchange', 'oxford_blue_M', :v, :st, 'swap S for M', now())
        RETURNING id
    """, s=session_id, u=unit, v=version, st=status).scalar()


def action(engine, proposal_id, reference, unit="u_1"):
    run(engine, """
        INSERT INTO actions (reference, proposal_id, order_unit_id, action, replacement_sku_id)
        VALUES (:r, :p, :u, 'exchange', 'oxford_blue_M')
    """, r=reference, p=proposal_id, u=unit)


def test_stock_cannot_go_negative(shop):
    db, _ = shop
    with pytest.raises(IntegrityError, match="skus_stock_check"):
        run(db, "UPDATE skus SET stock = stock - 1 WHERE id = 'oxford_blue_S'")


def test_one_proposal_commits_only_once(shop):
    db, session_id = shop
    p = proposal(db, session_id, 1)
    action(db, p, "RX-1")
    with pytest.raises(IntegrityError, match="actions_proposal_id_key"):
        action(db, p, "RX-2")


def test_one_active_action_per_unit(shop):
    db, session_id = shop
    action(db, proposal(db, session_id, 1), "RX-1")
    with pytest.raises(IntegrityError, match="one_active_action_per_unit"):
        action(db, proposal(db, session_id, 2), "RX-2")


def test_cancelled_action_frees_the_unit(shop):
    db, session_id = shop
    action(db, proposal(db, session_id, 1), "RX-1")
    run(db, "UPDATE actions SET status = 'cancelled'")
    action(db, proposal(db, session_id, 2), "RX-2")  # no error


def test_other_unit_is_independent(shop):
    db, session_id = shop
    action(db, proposal(db, session_id, 1, unit="u_1"), "RX-1", unit="u_1")
    action(db, proposal(db, session_id, 2, unit="u_2"), "RX-2", unit="u_2")  # no error


def test_only_one_live_proposal_per_session(shop):
    db, session_id = shop
    proposal(db, session_id, 1, status="open")
    with pytest.raises(IntegrityError, match="one_live_proposal_per_session"):
        proposal(db, session_id, 2, status="open")


def test_superseded_proposal_does_not_block_a_new_one(shop):
    db, session_id = shop
    proposal(db, session_id, 1, status="superseded")
    proposal(db, session_id, 2, status="open")  # no error


def test_exchange_needs_a_replacement_size(shop):
    db, session_id = shop
    with pytest.raises(IntegrityError, match="proposal_shape"):
        run(db, """
            INSERT INTO proposals (session_id, order_unit_id, action, version, summary)
            VALUES (:s, 'u_1', 'exchange', 1, 'swap')
        """, s=session_id)


def test_return_must_not_have_a_replacement(shop):
    db, session_id = shop
    with pytest.raises(IntegrityError, match="proposal_shape"):
        run(db, """
            INSERT INTO proposals (session_id, order_unit_id, action, replacement_sku_id, version, summary)
            VALUES (:s, 'u_1', 'return', 'oxford_blue_M', 1, 'return')
        """, s=session_id)


def test_confirmed_proposal_records_when(shop):
    db, session_id = shop
    with pytest.raises(IntegrityError, match="confirmed_has_time"):
        run(db, """
            INSERT INTO proposals (session_id, order_unit_id, action, version, status, summary)
            VALUES (:s, 'u_1', 'return', 1, 'confirmed', 'return')
        """, s=session_id)


def test_one_open_review_case_per_issue(shop):
    db, _ = shop
    insert = """
        INSERT INTO review_cases (shopper_id, order_unit_id, reason, question, trace_id)
        VALUES ('sh_1', :u, 'final_sale_dispute', 'manager said ok', 't_1')
    """
    run(db, insert, u="u_1")
    with pytest.raises(IntegrityError, match="one_open_case_per_issue"):
        run(db, insert, u="u_1")


def test_open_case_without_a_unit_is_also_deduplicated(shop):
    db, _ = shop
    insert = """
        INSERT INTO review_cases (shopper_id, reason, question, trace_id)
        VALUES ('sh_1', 'no_delivery_record', 'where is it', 't_1')
    """
    run(db, insert)
    with pytest.raises(IntegrityError, match="one_open_case_per_issue"):
        run(db, insert)
