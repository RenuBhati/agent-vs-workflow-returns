import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.clock import Clock
from app.services.errors import Refusal


@dataclass(frozen=True)
class Ctx:
    """Everything a service call needs. The shopper comes from the session, never from input."""

    engine: Engine
    session_id: uuid.UUID
    shopper_id: str
    trace_id: str
    clock: Clock


def start_session(
    engine: Engine, persona: str, assistant: str, config_version: str, clock: Clock
) -> Ctx:
    trace_id = uuid.uuid4().hex
    with engine.begin() as conn:
        shopper_id = conn.execute(
            text("SELECT id FROM shoppers WHERE persona_key = :p"), {"p": persona}
        ).scalar()
        if shopper_id is None:
            raise Refusal("not_found", f"no persona {persona!r}")
        session_id = conn.execute(
            text(
                "INSERT INTO sessions (shopper_id, assistant, config_version, trace_id) "
                "VALUES (:s, :a, :c, :t) RETURNING id"
            ),
            {"s": shopper_id, "a": assistant, "c": config_version, "t": trace_id},
        ).scalar()
    return Ctx(engine, session_id, shopper_id, trace_id, clock)


def load_session(engine: Engine, session_id: uuid.UUID, clock: Clock) -> Ctx:
    """Rebuild a Ctx from a stored session. The shopper always comes from the row."""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT shopper_id, trace_id FROM sessions WHERE id = :s"), {"s": session_id}
        ).first()
    if row is None:
        raise Refusal("not_found", "no such session")
    return Ctx(engine, session_id, row.shopper_id, row.trace_id, clock)
