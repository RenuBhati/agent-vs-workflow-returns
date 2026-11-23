"""The fixture loads, matches what scenarios rely on, and reset restores it."""

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from app.fixtures import Fixture, read_fixture, reset

FIXTURE = "two_shirts_v1"


def scalar(engine, sql):
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar()


def test_loads_the_expected_shop(db):
    fx = reset(db, FIXTURE)
    assert str(fx.today) == "2026-12-01"
    assert scalar(db, "SELECT count(*) FROM shoppers") == 5
    assert scalar(db, "SELECT count(*) FROM order_units") == 9


def test_edge_cases_are_present(db):
    reset(db, FIXTURE)
    # Last unit of blue M, none of blue L.
    assert scalar(db, "SELECT stock FROM skus WHERE id = 'oxford_blue_M'") == 1
    assert scalar(db, "SELECT stock FROM skus WHERE id = 'oxford_blue_L'") == 0
    # Ben: one order delivered exactly 30 days before today, one 31.
    assert scalar(db, "SELECT DATE '2026-12-01' - delivered_on FROM orders WHERE id = 'o_ben_1'") == 30
    assert scalar(db, "SELECT DATE '2026-12-01' - delivered_on FROM orders WHERE id = 'o_ben_2'") == 31
    # Chloe: a final-sale unit and an order with no delivery record.
    assert scalar(db, "SELECT final_sale FROM order_units WHERE id = 'u_chloe_jumper'") is True
    assert scalar(db, "SELECT delivered_on FROM orders WHERE id = 'o_chloe_2'") is None
    # Dev: two identical units in one order.
    assert scalar(db, "SELECT count(*) FROM order_units WHERE order_id = 'o_dev_1' "
                      "AND sku_id = 'tee_black_M'") == 2


def test_reset_restores_stock_and_clears_conversations(db):
    reset(db, FIXTURE)
    with db.begin() as conn:
        conn.execute(text("UPDATE skus SET stock = 0 WHERE id = 'oxford_blue_M'"))
        conn.execute(text(
            "INSERT INTO sessions (shopper_id, assistant, config_version, trace_id) "
            "VALUES ('sh_amira', 'agent', 'test', 't')"
        ))
    reset(db, FIXTURE)
    assert scalar(db, "SELECT stock FROM skus WHERE id = 'oxford_blue_M'") == 1
    assert scalar(db, "SELECT count(*) FROM sessions") == 0


def test_typo_in_a_fixture_is_rejected():
    data = read_fixture(FIXTURE).model_dump()
    data["skus"][0]["stok"] = data["skus"][0].pop("stock")
    with pytest.raises(ValidationError, match="stok"):
        Fixture.model_validate(data)


def test_unknown_sku_is_rejected():
    data = read_fixture(FIXTURE).model_dump()
    data["shoppers"][0]["orders"][0]["units"][0]["sku"] = "no_such_sku"
    with pytest.raises(ValidationError, match="unknown sku no_such_sku"):
        Fixture.model_validate(data)
