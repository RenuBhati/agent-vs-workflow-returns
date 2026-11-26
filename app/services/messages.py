"""The conversation transcript. Assistants record every message they show or receive."""

from typing import Literal

from sqlalchemy import text

from app.services.context import Ctx

QuestionKind = Literal["which_item", "which_size", "which_action", "confirm",
                       "choose_alternative", "offer_review", "other"]


def add_message(ctx: Ctx, role: Literal["shopper", "assistant"], body: str,
                question_kind: QuestionKind | None = None) -> int:
    """question_kind tags an assistant question so the simulated shopper can answer it."""
    if role == "shopper" and question_kind is not None:
        raise ValueError("only assistant messages carry a question kind")
    with ctx.engine.begin() as conn:
        return conn.execute(
            text("INSERT INTO messages (session_id, role, text, question_kind) "
                 "VALUES (:s, :r, :t, :q) RETURNING id"),
            {"s": ctx.session_id, "r": role, "t": body, "q": question_kind},
        ).scalar()
