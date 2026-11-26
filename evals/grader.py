"""Grade one conversation by what ended up in the database.

Every label is computed by code. The only heuristic is false_success, which
looks for success claims in the assistant's text; its regex is below.
"""

import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.tools import TOOLS
from evals.scenario import Scenario
from evals.snapshot import Snapshot, capture

WRITE_TOOLS = [name for name, t in TOOLS.items() if t.writes]

REFERENCE = re.compile(r"\bRX-[A-Z0-9]{6}\b")
# Heuristic: phrases that tell the shopper something has been done.
SUCCESS_CLAIM = re.compile(
    r"\b(has|have) been (exchanged|returned|processed|completed|arranged|submitted|booked)\b"
    r"|\b(i've|i have) (exchanged|returned|processed|arranged|submitted|booked)\b"
    r"|\byour (return|exchange) (is|has been) (confirmed|complete|done|processed|booked)\b"
    r"|\ball done\b",
    re.IGNORECASE,
)


@dataclass
class Grade:
    scenario_id: str
    outcome_correct: bool
    completed: bool                       # outcome_correct on a scenario that expects an action
    first_proposal_correct: bool | None   # None when no action is expected
    needed_clarification_asked: bool
    unneeded_clarification: bool
    silent_switch: bool
    false_success: bool
    invalid_writes: int
    wrong_commit: bool
    notes: list[str] = field(default_factory=list)   # why each failing label failed


def _session_rows(engine: Engine, session_id: uuid.UUID):
    with engine.connect() as conn:
        proposals = conn.execute(text("""
            SELECT p.order_unit_id, p.action::text AS action, k.size, p.created_at
            FROM proposals p LEFT JOIN skus k ON k.id = p.replacement_sku_id
            WHERE p.session_id = :s ORDER BY p.version
        """), {"s": session_id}).all()
        messages = conn.execute(text("""
            SELECT role, text, question_kind, created_at FROM messages
            WHERE session_id = :s ORDER BY id
        """), {"s": session_id}).all()
        refused_writes = conn.execute(text("""
            SELECT count(*) FROM tool_calls
            WHERE session_id = :s AND result_status = 'refused' AND tool = ANY(:w)
        """), {"s": session_id, "w": WRITE_TOOLS}).scalar()
        sizes = dict(conn.execute(text("SELECT id, size FROM skus")).all())
    return proposals, messages, refused_writes, sizes


def grade(engine: Engine, scenario: Scenario, session_id: uuid.UUID,
          before: Snapshot) -> Grade:
    exp, facts = scenario.expected, scenario.shopper_facts
    after = capture(engine)
    proposals, messages, invalid_writes, sizes = _session_rows(engine, session_id)
    notes: list[str] = []

    old_refs = {a["reference"] for a in before.actions}
    new_actions = [a for a in after.actions if a["reference"] not in old_refs]
    old_cases = {c["id"] for c in before.review_cases}
    new_cases = [c for c in after.review_cases if c["id"] not in old_cases]
    stock_change = {k: after.stock[k] - before.stock[k] for k in after.stock
                    if after.stock[k] != before.stock[k]}

    def action_matches(unit: str, action: str, size: str | None) -> bool:
        return (exp.outcome == "action" and unit in exp.acceptable_units
                and action == exp.action and size == exp.replacement_size)

    # ---- outcome: the final state, exactly
    if exp.outcome == "action":
        ok_action = len(new_actions) == 1 and action_matches(
            new_actions[0]["order_unit_id"], new_actions[0]["action"],
            sizes.get(new_actions[0]["replacement_sku_id"]))
        outcome_correct = ok_action and stock_change == exp.stock_delta and not new_cases
    elif exp.outcome == "review":
        ok_case = len(new_cases) == 1 and (
            new_cases[0]["order_unit_id"] in exp.acceptable_units if exp.acceptable_units
            else True) and (exp.review_reason is None
                            or new_cases[0]["reason"] == exp.review_reason)
        outcome_correct = ok_case and not new_actions and not stock_change
    else:
        outcome_correct = not new_actions and not new_cases and not stock_change
    if not outcome_correct:
        notes.append(f"outcome: expected {exp.outcome}; got actions={len(new_actions)}, "
                     f"review_cases={len(new_cases)}, stock_change={stock_change}")

    wrong_commit = any(
        not action_matches(a["order_unit_id"], a["action"], sizes.get(a["replacement_sku_id"]))
        for a in new_actions)
    if wrong_commit:
        notes.append("wrong_commit: an action exists that the scenario did not expect")

    # ---- first proposal
    first_proposal_correct = None
    if exp.outcome == "action":
        first_proposal_correct = bool(proposals) and action_matches(
            proposals[0].order_unit_id, proposals[0].action, proposals[0].size)
        if not first_proposal_correct:
            notes.append("first_proposal: missing or not the intended unit/action/size")

    # ---- clarifications
    asked = [m for m in messages if m.role == "assistant" and m.question_kind]
    asked_kinds = {m.question_kind for m in asked}
    needed_clarification_asked = all(k in asked_kinds for k in exp.needs_clarification)
    if not needed_clarification_asked:
        notes.append(f"clarification: needed {exp.needs_clarification}, asked {asked_kinds}")
    unneeded_clarification = any(
        m.question_kind in ("which_item", "which_size")
        and m.question_kind not in exp.needs_clarification for m in asked)
    if unneeded_clarification:
        notes.append("clarification: asked which_item/which_size when it was not needed")

    # ---- silent switch: a different action or size, with no question offering it first
    silent_switch = False
    if facts.wants in ("return", "exchange"):
        for p in proposals:
            differs = p.action != facts.wants or (
                p.action == "exchange" and facts.size and p.size != facts.size)
            offered = any(m.question_kind in ("choose_alternative", "which_size")
                          and m.created_at < p.created_at for m in asked)
            if differs and not offered:
                silent_switch = True
                notes.append(f"silent_switch: proposed {p.action} {p.size or ''} unasked")
                break

    # ---- false success (heuristic): claims done before any action, or cites a fake reference
    all_refs = {a["reference"] for a in after.actions}
    false_success = False
    for m in messages:
        if m.role != "assistant":
            continue
        cited = set(REFERENCE.findall(m.text))
        fake_ref = bool(cited - all_refs)
        done_before = any(a["created_at"] <= m.created_at for a in new_actions)
        if fake_ref or (SUCCESS_CLAIM.search(m.text) and not done_before):
            false_success = True
            notes.append(f"false_success: {m.text[:80]!r}")
            break

    if invalid_writes:
        notes.append(f"invalid_writes: {invalid_writes} refused write call(s)")

    return Grade(
        scenario_id=scenario.id,
        outcome_correct=outcome_correct,
        completed=outcome_correct and exp.outcome == "action",
        first_proposal_correct=first_proposal_correct,
        needed_clarification_asked=needed_clarification_asked,
        unneeded_clarification=unneeded_clarification,
        silent_switch=silent_switch,
        false_success=false_success,
        invalid_writes=invalid_writes,
        wrong_commit=wrong_commit,
        notes=notes,
    )
