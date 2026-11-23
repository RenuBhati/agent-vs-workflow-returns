"""Load a versioned fixture (db/fixtures/<name>.yaml) into the database.

reset() wipes every table, including sessions and actions, then loads the
fixture, so each test, scenario run and demo reset starts from the same shop.

Run it on the dev database with:  uv run python -m app.fixtures two_shirts_v1
"""

import sys
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import text
from sqlalchemy.engine import Engine

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "db" / "fixtures"

# Every table, for TRUNCATE. CASCADE handles foreign keys, so order does not matter.
ALL_TABLES = (
    "review_cases, actions, tool_calls, messages, proposals, sessions, "
    "order_units, orders, skus, styles, shoppers"
)


class Strict(BaseModel):
    # Unknown keys are errors, so a typo like "stok" fails loudly.
    model_config = ConfigDict(extra="forbid")


class Style(Strict):
    id: str
    name: str
    colour: str


class Sku(Strict):
    id: str
    style: str
    size: str
    stock: int


class Unit(Strict):
    id: str
    sku: str
    final_sale: bool = False


class Order(Strict):
    id: str
    placed_on: date
    delivered_on: date | None
    units: list[Unit]


class Shopper(Strict):
    id: str
    name: str
    persona: str
    orders: list[Order]


class Fixture(Strict):
    today: date
    styles: list[Style]
    skus: list[Sku]
    shoppers: list[Shopper]

    @model_validator(mode="after")
    def references_exist(self) -> "Fixture":
        """Catch broken references here, with a readable message, before touching the DB."""
        style_ids = {s.id for s in self.styles}
        sku_ids = {s.id for s in self.skus}
        for sku in self.skus:
            if sku.style not in style_ids:
                raise ValueError(f"sku {sku.id} names unknown style {sku.style}")
        unit_ids: set[str] = set()
        for shopper in self.shoppers:
            for order in shopper.orders:
                if order.delivered_on and order.delivered_on < order.placed_on:
                    raise ValueError(f"order {order.id} delivered before it was placed")
                if order.delivered_on and order.delivered_on > self.today:
                    raise ValueError(f"order {order.id} delivered after today ({self.today})")
                for unit in order.units:
                    if unit.sku not in sku_ids:
                        raise ValueError(f"unit {unit.id} names unknown sku {unit.sku}")
                    if unit.id in unit_ids:
                        raise ValueError(f"unit id {unit.id} used twice")
                    unit_ids.add(unit.id)
        return self


def read_fixture(name: str) -> Fixture:
    path = FIXTURE_DIR / f"{name}.yaml"
    return Fixture.model_validate(yaml.safe_load(path.read_text()))


def reset(engine: Engine, name: str) -> Fixture:
    """Wipe all tables and load the named fixture in one transaction."""
    fx = read_fixture(name)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {ALL_TABLES} RESTART IDENTITY CASCADE"))
        conn.execute(
            text("INSERT INTO styles (id, name, colour) VALUES (:id, :name, :colour)"),
            [s.model_dump() for s in fx.styles],
        )
        conn.execute(
            text("INSERT INTO skus (id, style_id, size, stock) VALUES (:id, :style, :size, :stock)"),
            [s.model_dump() for s in fx.skus],
        )
        for sh in fx.shoppers:
            conn.execute(
                text("INSERT INTO shoppers (id, name, persona_key) VALUES (:id, :name, :persona)"),
                {"id": sh.id, "name": sh.name, "persona": sh.persona},
            )
            for o in sh.orders:
                conn.execute(
                    text(
                        "INSERT INTO orders (id, shopper_id, placed_on, delivered_on) "
                        "VALUES (:id, :shopper, :placed_on, :delivered_on)"
                    ),
                    {"id": o.id, "shopper": sh.id, "placed_on": o.placed_on,
                     "delivered_on": o.delivered_on},
                )
                conn.execute(
                    text(
                        "INSERT INTO order_units (id, order_id, sku_id, final_sale) "
                        "VALUES (:id, :order_id, :sku, :final_sale)"
                    ),
                    [{**u.model_dump(), "order_id": o.id} for u in o.units],
                )
    return fx


if __name__ == "__main__":
    from app.config import settings
    from app.db import make_engine

    fixture_name = sys.argv[1] if len(sys.argv) > 1 else "two_shirts_v1"
    fx = reset(make_engine(settings.database_url), fixture_name)
    print(f"Loaded {fixture_name}: {len(fx.shoppers)} shoppers, today = {fx.today}")
