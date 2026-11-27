# Decisions

The main design choices, what else was considered, and why. Results are in [RESULTS.md](RESULTS.md), failures in [FAILURES.md](FAILURES.md).

## The comparison

**Same everything except who decides.** Both assistants share the model, the six business tools, the services, the database and the shop policy. The only difference is who chooses the next step: Python code (workflow) or the model (agent). If one wins, the reason is that choice, not better tools or a better prompt somewhere else.

**The workflow uses the model only to read.** It makes one structured-output call per shopper message and gets back intent, items, size, yes/no, and whether they want a person. A state machine does the rest. The model still does the language work, which is the part code can't do, and nothing else.

**No agent framework.** The agent loop is about 150 lines in `app/assistants/agent.py`. Every step is visible and logged, and there is nothing to rule out as a cause of a failure.

**A tool to end each turn.** The agent replies through `respond_to_shopper(text, question_kind)`. The tag says what kind of question it is asking, using the same tags the workflow uses, so both can be graded the same way. The tool is not a business tool and is not logged in `tool_calls`, so the tool counts stay comparable.

## Safety lives in the services

**The model never writes directly.** All writes go through `app/services`, which check ownership, policy, stock and confirmation. Postgres enforces the critical rules itself, through a check on stock, a unique index for one active action per item, and one commit per proposal. A wrong or manipulated assistant can propose something wrong, but it can't commit it.

**Confirm is a button, not a word.** The shopper presses Confirm on a specific proposal version, and the server records that click. Typing "yes" doesn't count. Commit is refused unless a matching confirmation exists. So the server can prove what the shopper saw and agreed to, and the evals can count the agent's attempts to commit without it.

**Confirm and commit are separate.** Confirm is the shopper's act and commit is the system's. Policy and stock are checked again at commit, because the day or the stock may have changed since the proposal.

**Refusals are data.** A tool that refuses returns a status and a reason code, not an exception. The model can read it and recover, and the evals can count it.

**A fixed list of review reasons.** Anything outside policy becomes a review case with a reason from a fixed list (`final_sale_dispute`, `no_delivery_record`, `policy_exception`, `action_conflict`, `shopper_request`, `assistant_stuck`). That makes them countable, and lets the grader check that the right one was used.

## The model

**A local 4B model (`qwen3.5:4b` through Ollama).** It is free to run hundreds of eval conversations, and it fits an 8 GB laptop. A small model is also where the planning-versus-reading difference should show most. A hosted model would change the numbers, not the method.

**Pinned and reproducible.** The model digest is checked at start-up. Temperature is 0.2 and the seed is 7, with a retry on seed+1 if the model makes a bad tool call. Each agent step gets its own seed offset, so a failed step isn't replayed exactly.

**Errors are returned as data.** Timeouts, bad tool calls and server errors come back as a reply with an error field. They are logged in `model_calls` and counted, instead of crashing the conversation.

**A workaround for tools with no parameters.** Given a tool with no arguments, the model often produced a call Ollama couldn't parse (HTTP 500). Each such tool gets an optional `note` argument ("why you are calling this tool"), which is removed before the call is run. Malformed calls went from frequent to zero.

## The evals

**Graded on final state.** The main measure is what ended up in the database: actions, review cases and stock. What the assistant said comes second. A polite reply that changed the wrong item is a failure.

**Checks for what the final state can't show.** These catch a correct end state reached a bad way: a silent switch to something the shopper didn't ask for, claiming success without a reference, refused or bad write calls, unneeded or missing questions, a proposal left open, and loops. A conversation is "clean" only if none of these fire.

**A rule-based simulated shopper.** It answers from private facts and presses Confirm only on what it wants. It has no model, so both assistants face exactly the same shopper, and a difference in results comes from the assistant. The cost is realism: its replies are canned (see [FAILURES.md](FAILURES.md) for where that mattered).

**Scripted moves for mid-conversation changes.** Corrections, cancels and "yes" typed in chat are scripted moves that fire on a trigger. Each proposal is graded against what the shopper wanted when it was made. So an authorised correction isn't a silent switch, and the first proposal is still judged against the first request.

**Graded by tag; the shopper reads the text.** Clarification checks use the question tag, so a wrong tag costs the assistant on that check. For an `other` question, the shopper reads the text and answers what it actually asks, so a wrong tag doesn't also stall the conversation. That way the tag error is counted once, not twice.

**Dev and held-out splits.** Both assistants were tuned only on the 13 dev scenarios. The 17 held-out scenarios were run once, after the code was frozen at commit `09dd931`, and nothing was changed afterwards. Each scenario has 3 wordings.

**The grader is tested too.** Scripted conversations with known mistakes (wrong item, silent switch, false success, and others) check that the grader labels each one correctly.

**No model calls in CI.** CI runs the backend tests, the grader self-tests and lint on every push. Model evals are run by hand, because they are slow and vary from run to run.

## Storage

**Postgres, not SQLite.** Concurrent writes and row locks are part of the problem, such as two shoppers racing for the last unit.

**Plain SQL migrations.** The constraints are the design, and plain SQL keeps them readable.

**One row per proposal version.** Confirm binds to an immutable row, and the history is kept for replays.

**Conversation state in `sessions.state`.** Both assistants are stateless between requests and reload their state from the database. A restart doesn't lose a conversation.
