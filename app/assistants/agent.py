"""The tool-using agent.

The model decides what to do. Each turn it may call the six business tools
(through call_tool, exactly like the workflow) as often as it likes, up to a
step limit, and ends the turn by calling respond_to_shopper. That reply tool
gives each answer a question kind, the same tags the workflow uses, so both
assistants are graded the same way. It is not a business tool and is not
logged in tool_calls, so tool counts stay comparable.

The conversation, including tool calls and results, is kept in sessions.state
so the next turn continues it.
"""

import json

from app.assistants.base import Reply, load_state, save_state, say
from app.llm import LLM, record_call
from app.services.context import Ctx
from app.services.messages import add_message
from app.services.proposals import live_proposal
from app.tools import TOOLSET_VERSION, call_tool, tool_schemas

NAME = "agent"
PROMPT_VERSION = "agent-v1"
MAX_STEPS = 8   # model calls per turn

QUESTION_KINDS = ["which_item", "which_size", "which_action", "confirm",
                  "choose_alternative", "offer_review", "other", "none"]

RESPOND = {
    "name": "respond_to_shopper",
    "description": "Send your message to the shopper. This ends your turn. Every reply to "
                   "the shopper must use this tool.",
    "input_schema": {
        "type": "object",
        "required": ["text", "question_kind"],
        "properties": {
            "text": {"type": "string", "description": "What the shopper will read."},
            "question_kind": {
                "type": "string", "enum": QUESTION_KINDS,
                "description": "What you are asking: which_item (which item they mean), "
                               "which_size (which size they want), which_action (return or "
                               "exchange), confirm (asking them to press Confirm), "
                               "choose_alternative (offering other sizes, a return or "
                               "nothing), offer_review (offering to pass it to a person), "
                               "other (any other question), none (no question; you are "
                               "done)."},
        },
    },
}

SYSTEM = """You are the returns and exchanges assistant for a clothing shop. Today is {today}.
Follow these steps.
1. Call list_orders first to see the shopper's items.
2. Work out which item they mean. If more than one item fits, ask which one
   (which_item). Items in the same order with the same name, colour and size are
   identical: pick either without asking.
3. If they have not said whether they want a return or an exchange, ask (which_action).
   An exchange needs a size; if they gave none, call check_options and ask which size,
   listing the sizes in stock (which_size).
4. Call check_options before proposing. If the size they asked for is out of stock, the
   same as theirs, or not made, explain and offer the sizes in stock, a return, or leaving
   it (choose_alternative). Never change the item, action or size without the shopper
   choosing it.
5. If the item is final sale, past its return window, has no delivery record, or already
   has a return or exchange, explain why and offer to pass it to a person
   (offer_review). Call request_review only if they agree or ask for a person, with the
   most specific reason.
6. When it is eligible, call propose. Then tell the shopper the summary and ask them to
   press the Confirm button (confirm). Typing "yes" is not pressing Confirm.
7. When told the shopper pressed Confirm, call commit_action with that proposal_id. Say
   it is done only after commit_action returns a reference, and give the reference.
8. You only handle returns and exchanges of items bought here. For anything else, say so
   briefly (none).
Reply to the shopper only with respond_to_shopper."""

STUCK = ("Sorry, I'm having trouble sorting this out. Would you like me to pass it to a "
         "person to review?")


class AgentAssistant:
    name = NAME

    def __init__(self, llm: LLM):
        self.llm = llm
        self.config_version = f"{PROMPT_VERSION}+{TOOLSET_VERSION}+{llm.name}"
        self.tools = tool_schemas() + [RESPOND]

    def handle(self, ctx: Ctx, message: str, turn: int) -> Reply:
        add_message(ctx, "shopper", message)
        return self._run(ctx, {"role": "user", "content": message}, turn)

    def on_confirm(self, ctx: Ctx, proposal_id: str, turn: int) -> Reply:
        event = (f"[The shopper pressed Confirm on proposal_id {proposal_id}. Carry it out "
                 "now.]")
        return self._run(ctx, {"role": "user", "content": event}, turn)

    # ------------------------------------------------------------ the loop

    def _run(self, ctx: Ctx, incoming: dict, turn: int) -> Reply:
        state = load_state(ctx)
        history = state.get("history") or [
            {"role": "system", "content": SYSTEM.format(today=ctx.clock.today().isoformat())}]
        history.append(incoming)
        try:
            return self._steps(ctx, history, turn)
        finally:
            save_state(ctx, {**state, "history": history})

    def _steps(self, ctx: Ctx, history: list[dict], turn: int) -> Reply:
        for _ in range(MAX_STEPS):
            reply = self.llm.chat(history, tools=self.tools)
            record_call(ctx, self.llm, turn, "agent_step", reply)
            if reply.error:
                continue   # logged; try the step again within the limit

            if not reply.tool_calls:
                # Prose instead of the reply tool: show it. The tag only lets the simulated
                # shopper answer; it earns nothing. A waiting proposal means "press Confirm".
                text_ = reply.content.strip() or STUCK
                history.append({"role": "assistant", "content": text_})
                p = live_proposal(ctx)
                kind = ("confirm" if p and p.status == "open"
                        else "other" if "?" in text_ else None)
                return say(ctx, text_, kind)

            history.append({"role": "assistant", "content": reply.content or "",
                            "tool_calls": [{"function": c} for c in reply.tool_calls]})
            for call in reply.tool_calls:
                if call["name"] == RESPOND["name"]:
                    return self._respond(ctx, history, call["arguments"])
                result = call_tool(ctx, call["name"], call["arguments"], turn)
                history.append({"role": "tool", "tool_name": call["name"],
                                "content": json.dumps(result.as_dict(), default=str)})

        history.append({"role": "assistant", "content": STUCK})
        return say(ctx, STUCK, "offer_review")

    def _respond(self, ctx: Ctx, history: list[dict], args: dict) -> Reply:
        text_ = str(args.get("text") or "").strip() or STUCK
        kind = args.get("question_kind")
        kind = None if kind in (None, "none") or kind not in QUESTION_KINDS else kind
        history.append({"role": "tool", "tool_name": RESPOND["name"],
                        "content": '{"status": "sent"}'})
        return say(ctx, text_, kind)
