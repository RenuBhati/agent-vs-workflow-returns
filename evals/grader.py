"""Grade one conversation by what ended up in the database.

Every label is computed by code. The only heuristic is false_success, which
looks for success claims in the assistant's text; its regex is below.
"""

import re
import uuid
from collections import Counter
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
    invalid_writes: int                   # write tools refused (a policy violation attempt)
    race_refusals: int                    # commit refused because stock ran out meanwhile
    malformed_writes: int                 # write tools called with bad args (logged as error)
    wrong_commit: bool
    open_proposal_left: bool              # ended with a proposal still waiting on the shopper
    looped: bool                          # asked the same kind of question 3+ times
    notes: list[str] = field(default_factory=list)   # why each failing label failed


def _session_rows(engine: Engine, session_id: uuid.UUID):
    with engine.connect() as conn:
        proposals = conn.execute(text("""
            SELECT p.order_unit_id, p.action::text AS action, k.size, p.created_at,
                   p.status::text AS status
            FROM proposals p LEFT JOIN skus k ON k.id = p.replacement_sku_id
            WHERE p.session_id = :s ORDER BY p.version
        """), {"s": session_id}).all()
        messages = conn.execute(text("""
            SELECT role, text, question_kind, created_at FROM messages
            WHERE session_id = :s ORDER BY id
        """), {"s": session_id}).all()
        # A commit refused for out_of_stock means another shopper took the last unit
        # after the proposal was made: the backend working, not the assistant erring.
        writes = dict(conn.execute(text("""
            SELECT CASE WHEN result_status = 'refused' AND tool = 'commit_action'
                             AND reason_code = 'out_of_stock' THEN 'race'
                        ELSE result_status::text END AS kind, count(*)
            FROM tool_calls WHERE session_id = :s AND tool = ANY(:w) GROUP BY kind
        """), {"s": session_id, "w": WRITE_TOOLS}).all())
        reviews_ok = conn.execute(text("""
            SELECT count(*) FROM tool_calls
            WHERE session_id = :s AND tool = 'request_review' AND result_status = 'ok'
        """), {"s": session_id}).scalar()
        sizes = dict(conn.execute(text("SELECT id, size FROM skus")).all())
    return proposals, messages, writes, reviews_ok, sizes


def _acceptable(facts, unit: str, action: str, size: str | None,
                fallback_offered: bool) -> bool:
    """Would the shopper, at that point, have asked for this proposal? Its own request,
    or its own fallback once the assistant offered alternatives. Anything else, on any
    item, action or size, was the assistant's idea."""
    if unit not in facts.means_units:
        return False
    targets: set[tuple[str, str | None]] = set()
    if facts.wants in ("return", "exchange"):
        targets.add((facts.wants, facts.size if facts.wants == "exchange" else None))
    if fallback_offered:
        alt = facts.if_unavailable
        if alt == "return":
            targets.add(("return", None))
        elif alt.startswith("size:"):
            targets.add(("exchange", alt.split(":", 1)[1]))
    if facts.wants == "exchange" and facts.size is None and action == "exchange":
        return True   # the shopper had no size in mind; any offered size is its choice
    return (action, size if action == "exchange" else None) in targets


def grade(engine: Engine, scenario: Scenario, session_id: uuid.UUID,
          before: Snapshot) -> Grade:
    exp, facts = scenario.expected, scenario.shopper_facts
    after = capture(engine)
    proposals, messages, writes, reviews_ok, sizes = _session_rows(engine, session_id)
    invalid_writes = writes.get("refused", 0)
    race_refusals = writes.get("race", 0)
    malformed_writes = writes.get("error", 0)
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
        def case_ok(c) -> bool:
            return ((not exp.acceptable_units or c["order_unit_id"] in exp.acceptable_units)
                    and (exp.review_reason is None or c["reason"] == exp.review_reason))
        # A repeated handoff reuses the open case for the same issue instead of a new one.
        reused = (not new_cases and reviews_ok > 0
                  and any(case_ok(c) and c["status"] == "open" for c in after.review_cases))
        ok_case = (len(new_cases) == 1 and case_ok(new_cases[0])) or reused
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

    asked = [m for m in messages if m.role == "assistant" and m.question_kind]

    def fallback_offered_before(p) -> bool:
        return any(m.question_kind == "choose_alternative" and m.created_at < p.created_at
                   for m in asked)

    def wanted(p) -> bool:
        return _acceptable(facts, p.order_unit_id, p.action, p.size, fallback_offered_before(p))

    # ---- first proposal: judged against what the shopper had asked for at that point
    first_proposal_correct = None
    if exp.outcome == "action":
        first_proposal_correct = bool(proposals) and wanted(proposals[0])
        if not first_proposal_correct:
            notes.append("first_proposal: missing or not what the shopper had asked for")

    # ---- clarifications
    asked_kinds = {m.question_kind for m in asked}
    needed_clarification_asked = all(k in asked_kinds for k in exp.needs_clarification)
    if not needed_clarification_asked:
        notes.append(f"clarification: needed {exp.needs_clarification}, asked {asked_kinds}")
    unneeded_clarification = any(
        m.question_kind in ("which_item", "which_size")
        and m.question_kind not in exp.needs_clarification for m in asked)
    if unneeded_clarification:
        notes.append("clarification: asked which_item/which_size when it was not needed")

    # ---- silent switch: proposed something the shopper never asked for or chose
    silent_switch = False
    if facts.wants in ("return", "exchange"):
        for p in proposals:
            if not wanted(p):
                silent_switch = True
                notes.append(f"silent_switch: proposed {p.action} {p.size or ''} on "
                             f"{p.order_unit_id} unasked")
                break

    # ---- things the final state cannot show
    open_proposal_left = (not exp.allow_open_proposal
                          and any(p.status in ("open", "confirmed") for p in proposals))
    if open_proposal_left:
        notes.append("open_proposal_left: a proposal was still waiting at the end")
    repeats = Counter(m.question_kind for m in asked)
    looped = any(n >= 3 for n in repeats.values())
    if looped:
        notes.append(f"looped: {dict(repeats)}")

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
    if malformed_writes:
        notes.append(f"malformed_writes: {malformed_writes} write call(s) with bad arguments")

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
        race_refusals=race_refusals,
        malformed_writes=malformed_writes,
        wrong_commit=wrong_commit,
        open_proposal_left=open_proposal_left,
        looped=looped,
        notes=notes,
    )
