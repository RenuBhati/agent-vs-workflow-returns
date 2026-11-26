"""HTTP API for the web page and the scenario runner.

Each endpoint is a thin wrapper around a service: rules live in app/services.
The session ID in the URL identifies the shopper; nothing in a request can
name a different one.

Run locally:  uv run uvicorn app.api:app --reload
"""

import uuid
from collections.abc import Callable
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.assistants.agent import AgentAssistant
from app.assistants.base import Assistant, Reply
from app.assistants.workflow import WorkflowAssistant
from app.clock import Clock, FixedClock
from app.config import settings
from app.db import make_engine
from app.fixtures import read_fixture, reset
from app.llm import OllamaLLM
from app.services.context import Ctx, load_session, start_session
from app.services.errors import Refusal
from app.services.orders import list_orders
from app.services.proposals import confirm, decline, live_proposal
from app.tools import TOOLSET_VERSION, unit_json


class StartSession(BaseModel):
    persona: str
    assistant: Literal["workflow", "agent"]


class ConfirmBody(BaseModel):
    version: int


class ShopperMessage(BaseModel):
    text: str


# Builds the assistant for a session. Injected so tests can use a fake model.
Assistants = dict[str, Callable[[], Assistant]]


def default_assistants() -> Assistants:
    # The agent's context holds six tool schemas plus tool results, so it needs more room.
    return {"workflow": lambda: WorkflowAssistant(OllamaLLM()),
            "agent": lambda: AgentAssistant(OllamaLLM(num_ctx=8192))}


def create_app(engine: Engine, clock: Clock, allow_reset: bool = False,
               fixture: str = settings.fixture,
               assistants: Assistants | None = None) -> FastAPI:
    app = FastAPI(title="Returns assistant")
    assistants = default_assistants() if assistants is None else assistants

    @app.exception_handler(Refusal)
    async def refusal_to_http(request: Request, r: Refusal) -> JSONResponse:
        status = 404 if r.code == "not_found" else 409
        return JSONResponse(status_code=status, content={"code": r.code, "data": r.data})

    def ctx(session_id: uuid.UUID) -> Ctx:
        return load_session(engine, session_id, clock)

    def assistant_for(c: Ctx) -> Assistant:
        with engine.connect() as conn:
            name = conn.execute(text("SELECT assistant FROM sessions WHERE id = :s"),
                                {"s": c.session_id}).scalar()
        if name not in assistants:
            raise HTTPException(status_code=501, detail=f"assistant {name!r} is not available")
        return assistants[name]()

    def next_turn(c: Ctx) -> int:
        # Every assistant step writes exactly one assistant message, so this numbers
        # each step once, and tool and model logs line up with the transcript.
        with engine.connect() as conn:
            return 1 + conn.execute(
                text("SELECT count(*) FROM messages WHERE session_id = :s "
                     "AND role = 'assistant'"), {"s": c.session_id}).scalar()

    def proposal_json(c: Ctx) -> dict | None:
        p = live_proposal(c)
        if p is None:
            return None
        return {"proposal_id": str(p.id), "version": p.version, "summary": p.summary,
                "status": p.status, "order_unit_id": p.order_unit_id, "action": p.action,
                "replacement_size": p.replacement_size}

    def turn_json(c: Ctx, r: Reply) -> dict:
        return {"reply": r.text, "question_kind": r.kind, "proposal": proposal_json(c)}

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.post("/sessions", status_code=201)
    def new_session(body: StartSession) -> dict:
        factory = assistants.get(body.assistant)
        version = factory().config_version if factory else TOOLSET_VERSION
        c = start_session(engine, body.persona, body.assistant, version, clock)
        return {"session_id": str(c.session_id), "trace_id": c.trace_id,
                "today": clock.today().isoformat()}

    @app.get("/sessions/{session_id}/orders")
    def orders(session_id: uuid.UUID) -> dict:
        return {"units": [unit_json(u) for u in list_orders(ctx(session_id))]}

    @app.get("/sessions/{session_id}/proposal")
    def proposal(session_id: uuid.UUID) -> dict:
        return {"proposal": proposal_json(ctx(session_id))}

    @app.get("/sessions/{session_id}/messages")
    def transcript(session_id: uuid.UUID) -> dict:
        c = ctx(session_id)
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT role, text, question_kind FROM messages WHERE session_id = :s "
                "ORDER BY id"), {"s": c.session_id}).all()
        return {"messages": [dict(r._mapping) for r in rows]}

    @app.post("/sessions/{session_id}/messages")
    def send_message(session_id: uuid.UUID, body: ShopperMessage) -> dict:
        c = ctx(session_id)
        a = assistant_for(c)
        return turn_json(c, a.handle(c, body.text, next_turn(c)))

    @app.post("/sessions/{session_id}/proposals/{proposal_id}/confirm")
    def confirm_proposal(session_id: uuid.UUID, proposal_id: uuid.UUID,
                         body: ConfirmBody) -> dict:
        # The click is recorded first, by the service; only then may the assistant commit.
        c = ctx(session_id)
        status = confirm(c, proposal_id, body.version)
        reply = assistant_for(c).on_confirm(c, str(proposal_id), next_turn(c))
        return {"status": status, **turn_json(c, reply)}

    @app.post("/sessions/{session_id}/proposals/{proposal_id}/decline")
    def decline_proposal(session_id: uuid.UUID, proposal_id: uuid.UUID) -> dict:
        return {"status": decline(ctx(session_id), proposal_id)}

    @app.post("/reset")
    def reset_shop() -> dict:
        if not allow_reset:
            raise HTTPException(status_code=403, detail="reset is disabled")
        fx = reset(engine, fixture)
        return {"fixture": fixture, "today": fx.today.isoformat()}

    return app


def _production_app() -> FastAPI:
    # The demo shop has a fixed "today" so its date rules always line up.
    return create_app(make_engine(settings.database_url),
                      FixedClock(read_fixture(settings.fixture).today),
                      allow_reset=settings.allow_reset)


app = _production_app()
