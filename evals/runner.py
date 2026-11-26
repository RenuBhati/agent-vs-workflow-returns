"""Run scenarios end to end and grade them.

For each scenario, each wording of its opening, and each assistant:
reset the shop, snapshot it, play the conversation through the real HTTP API
with the simulated shopper, grade the final database state, and write one
JSON line.

    uv run python -m evals.runner --split dev --assistant workflow
    uv run python -m evals.runner --scenario exch-blue-medium --repeats 3
"""

import argparse
import json
import time
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.api import Assistants, create_app, default_assistants
from app.clock import FixedClock
from app.fixtures import reset
from evals.grader import grade
from evals.scenario import Scenario, load_split
from evals.shopper import SimulatedShopper
from evals.snapshot import capture

MAX_SHOPPER_TURNS = 8
RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"


def run_one(engine: Engine, scenario: Scenario, assistant: str, assistants: Assistants,
            opening: str | None = None) -> dict:
    fx = reset(engine, scenario.fixture)
    before = capture(engine)
    client = TestClient(create_app(engine, FixedClock(fx.today), assistants=assistants),
                        raise_server_exceptions=True)
    sid = client.post("/sessions", json={"persona": scenario.persona,
                                         "assistant": assistant}).json()["session_id"]
    shopper = SimulatedShopper(scenario.shopper_facts, opening or scenario.opening)

    started = time.perf_counter()
    move, turns, ended = shopper.first(), 0, "assistant_finished"
    while True:
        if turns >= MAX_SHOPPER_TURNS:
            ended = "turn_limit"
            break
        turns += 1
        if move.kind == "say":
            r = client.post(f"/sessions/{sid}/messages", json={"text": move.text}).json()
        elif move.kind == "confirm":
            p = r["proposal"]
            r = client.post(f"/sessions/{sid}/proposals/{p['proposal_id']}/confirm",
                            json={"version": p["version"]}).json()
        else:
            turns -= 1
            break
        move = shopper.respond(r.get("question_kind"), r.get("proposal"), r.get("reply", ""))
    seconds = time.perf_counter() - started

    g = grade(engine, scenario, uuid.UUID(sid), before)
    return {
        "scenario": scenario.id, "assistant": assistant, "opening": shopper.opening,
        "session_id": sid, "ended": ended, "shopper_turns": turns,
        "seconds": round(seconds, 1), **_usage(engine, sid), **asdict(g),
        # The next run resets the database, so keep what is needed to inspect this one.
        "transcript": _transcript(engine, sid),
    }


def _transcript(engine: Engine, sid: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT 'message' AS type, id, created_at, role AS who, question_kind AS detail,
                   text AS body
            FROM messages WHERE session_id = :s
            UNION ALL
            SELECT 'tool', id, created_at, tool, result_status::text || coalesce(
                       ':' || reason_code, ''), args::text
            FROM tool_calls WHERE session_id = :s
            ORDER BY created_at, type, id
        """), {"s": sid}).all()
    return [{"type": r.type, "who": r.who, "detail": r.detail, "body": r.body} for r in rows]


def _usage(engine: Engine, sid: str) -> dict:
    with engine.connect() as conn:
        cfg = conn.execute(text("SELECT config_version FROM sessions WHERE id = :s"),
                           {"s": sid}).scalar()
        tools = conn.execute(text("SELECT count(*) FROM tool_calls WHERE session_id = :s"),
                             {"s": sid}).scalar()
        m = conn.execute(text("""
            SELECT count(*) AS calls, coalesce(sum(prompt_tokens), 0) AS prompt_tokens,
                   coalesce(sum(output_tokens), 0) AS output_tokens,
                   coalesce(sum(duration_ms), 0) AS model_ms,
                   count(*) FILTER (WHERE error IS NOT NULL) AS model_errors
            FROM model_calls WHERE session_id = :s"""), {"s": sid}).one()
    return {"config_version": cfg, "tool_calls": tools, "model_calls": m.calls,
            "prompt_tokens": m.prompt_tokens, "output_tokens": m.output_tokens,
            "model_seconds": round(m.model_ms / 1000, 1), "model_errors": m.model_errors}


def main() -> None:
    from app.config import settings
    from app.db import make_engine

    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev")
    ap.add_argument("--scenario", help="run only this scenario id")
    ap.add_argument("--assistant", default="workflow", choices=["workflow", "agent"])
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--no-wordings", action="store_true", help="opening line only")
    a = ap.parse_args()

    engine = make_engine(settings.database_url)
    scenarios = [s for s in load_split(a.split) if not a.scenario or s.id == a.scenario]
    if not scenarios:
        raise SystemExit("no matching scenarios")
    RUNS_DIR.mkdir(exist_ok=True)
    out = RUNS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{a.split}-{a.assistant}.jsonl"
    assistants = default_assistants()

    with out.open("w") as fh:
        for s in scenarios:
            openings = [s.opening] + ([] if a.no_wordings else s.wordings)
            for opening in openings:
                for _ in range(a.repeats):
                    row = run_one(engine, s, a.assistant, assistants, opening)
                    fh.write(json.dumps(row) + "\n")
                    fh.flush()
                    mark = "PASS" if row["outcome_correct"] else "FAIL"
                    print(f"{mark} {s.id} | {opening[:45]!r} | {row['shopper_turns']} turns, "
                          f"{row['seconds']}s, {row['model_errors']} model errors"
                          + ("".join(f"\n     {n}" for n in row["notes"])), flush=True)
    print(f"results: {out}")


if __name__ == "__main__":
    main()
