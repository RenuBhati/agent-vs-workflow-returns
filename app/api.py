"""HTTP API for the web page and the scenario runner.

Each endpoint is a thin wrapper around a service: rules live in app/services.
The session ID in the URL identifies the shopper; nothing in a request can
name a different one.

Run locally:  uv run uvicorn app.api:app --reload
"""

import uuid
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.engine import Engine

from app.clock import Clock, FixedClock
from app.config import settings
from app.db import make_engine
from app.fixtures import read_fixture, reset
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


def create_app(engine: Engine, clock: Clock, allow_reset: bool = False,
               fixture: str = settings.fixture) -> FastAPI:
    app = FastAPI(title="Returns assistant")

    @app.exception_handler(Refusal)
    async def refusal_to_http(request: Request, r: Refusal) -> JSONResponse:
        status = 404 if r.code == "not_found" else 409
        return JSONResponse(status_code=status, content={"code": r.code, "data": r.data})

    def ctx(session_id: uuid.UUID) -> Ctx:
        return load_session(engine, session_id, clock)

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.post("/sessions", status_code=201)
    def new_session(body: StartSession) -> dict:
        c = start_session(engine, body.persona, body.assistant, TOOLSET_VERSION, clock)
        return {"session_id": str(c.session_id), "trace_id": c.trace_id,
                "today": clock.today().isoformat()}

    @app.get("/sessions/{session_id}/orders")
    def orders(session_id: uuid.UUID) -> dict:
        return {"units": [unit_json(u) for u in list_orders(ctx(session_id))]}

    @app.get("/sessions/{session_id}/proposal")
    def proposal(session_id: uuid.UUID) -> dict:
        p = live_proposal(ctx(session_id))
        if p is None:
            return {"proposal": None}
        return {"proposal": {"proposal_id": str(p.id), "version": p.version,
                             "summary": p.summary, "status": p.status,
                             "order_unit_id": p.order_unit_id, "action": p.action,
                             "replacement_size": p.replacement_size}}

    @app.post("/sessions/{session_id}/proposals/{proposal_id}/confirm")
    def confirm_proposal(session_id: uuid.UUID, proposal_id: uuid.UUID,
                         body: ConfirmBody) -> dict:
        return {"status": confirm(ctx(session_id), proposal_id, body.version)}

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
