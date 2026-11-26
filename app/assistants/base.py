"""Shared plumbing for both assistants: replies, and per-session state."""

import json
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text

from app.services.context import Ctx
from app.services.messages import QuestionKind, add_message


@dataclass(frozen=True)
class Reply:
    text: str
    kind: QuestionKind | None = None   # None = no question; the conversation can end


class Assistant(Protocol):
    """What the API and runner need from either assistant."""

    name: str
    config_version: str

    def handle(self, ctx: Ctx, message: str, turn: int) -> Reply: ...

    def on_confirm(self, ctx: Ctx, proposal_id: str, turn: int) -> Reply: ...


def say(ctx: Ctx, text_: str, kind: QuestionKind | None = None) -> Reply:
    add_message(ctx, "assistant", text_, kind)
    return Reply(text_, kind)


def load_state(ctx: Ctx) -> dict:
    with ctx.engine.connect() as conn:
        return conn.execute(text("SELECT state FROM sessions WHERE id = :s"),
                            {"s": ctx.session_id}).scalar() or {}


def save_state(ctx: Ctx, state: dict) -> None:
    with ctx.engine.begin() as conn:
        conn.execute(text("UPDATE sessions SET state = CAST(:st AS jsonb) WHERE id = :s"),
                     {"s": ctx.session_id, "st": json.dumps(state)})
