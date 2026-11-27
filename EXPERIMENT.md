# Experiment: does a better tool result help the agent?

The held-out run ([RESULTS.md](RESULTS.md)) showed the agent's biggest weakness is the policy "no": an item that is final sale, past the window, missing a delivery record or already actioned. In those cases it often filed a review the shopper never asked for, picked the wrong review reason, or missed an existing action ([FAILURES.md](FAILURES.md), A2 and A3).

This experiment changes one thing, the design of one tool, and keeps everything else fixed.

## What changed

`check_options` already returned whether the item is eligible and why not. Under **tools-v2** it also returns a `next_step`, so the agent no longer has to work out what to do from the reason alone:

| Situation | `next_step` |
|---|---|
| Final sale, past the window, no delivery record, already actioned | Don't propose. Offer a review by a person, using this exact review reason. File it only if the shopper says yes. |
| Out of stock, same size, unknown size | Offer the alternatives. |
| Exchange with no size given | Ask which size. |
| Eligible | Propose now. |

The model (qwen3.5:4b, same digest), the agent-v3 prompt, the other tools, the shopper and the grader are unchanged. The toolset is part of `config_version` (`agent-v3+tools-v1+...` vs `agent-v3+tools-v2+...`). tools-v1 stays the default, so RESULTS.md still reproduces.

## Scenarios

The held-out failures are what suggested tools-v2, so scoring it on held-out would favour it. The experiment uses a fresh `exp` split instead: 12 scenarios, 3 wordings each, 36 conversations per toolset.

- 6 policy "no" cases where the shopper accepts or declines the offered review (final sale jumper and jacket, expired linen shirt).
- 2 where the item already has an exchange or a return.
- 2 size problems (out of stock, same size) and 2 ordinary requests, to catch regressions.

As a check on the scenarios, the workflow got 36/36 correct on `exp`.

## Results

| | tools-v1 | tools-v2 |
|---|---|---|
| Correct final state | 28/36 | **31/36** |
| Clean (correct and no grader notes) | 10/36 | **17/36** |
| Asked the question the scenario needed | 13/30 | **19/30** |
| Review filed although the shopper declined it (of 9) | 4 | 2 |
| Writes refused by the services | 5 | 2 |
| Silent switch | 6 | 4 |
| False success | 0 | 0 |
| Model calls per conversation | 5.9 | 5.6 |
| Prompt tokens per conversation | 13,934 | 13,624 |

By group, correct of the conversations in it:

| Group | tools-v1 | tools-v2 |
|---|---|---|
| Review offered, shopper accepts (9) | 6 | 8 |
| Review offered, shopper declines (9) | 5 | 7 |
| Item already actioned (6) | 5 | 5 |
| Size problem (6) | 6 | 5 |
| Ordinary (6) | 6 | 6 |

Seconds per conversation fell from 210 to 128, but model calls and tokens barely moved. The tools-v1 run hit memory pressure badly enough that Docker stopped (its last scenario was rerun), so the time difference is mostly the laptop, not the tools.

## What went wrong under tools-v2

- **The agent still has to read `next_step`, and sometimes doesn't.** Two decline conversations still filed a review, and two conversations that needed a review ended with none filed.
- **Wrong arguments bypass the guidance.** The one regression (same-size exchange) came from the agent calling `check_options` with size M, then with no size, so the tool never reported the same-size problem. After several rounds of the shopper repeating "size S" it filed a review nobody asked for.
- **Tags are still a weak spot.** Many offers were tagged `other` instead of `offer_review`, which the grader counts as the needed question not asked. The conversation still works, because the shopper reads the text of `other` questions.

## What it means

Putting the next step in the tool result helped: clean conversations went from 10 to 17 of 36, and the agent got the policy "no" cases right more often. It cost nothing extra in tokens.

It does not close the gap with the workflow. The workflow gets 36/36 on the same scenarios because its state machine does what `next_step` describes every time, while the agent follows it most of the time. In effect tools-v2 moves a little of the workflow's logic into the tool, and the agent gets better as it does.

## Caveats

- One run of 36 conversations per toolset. Three more correct conversations is suggestive; the clean and needed-question gains are larger but still from one run.
- The `exp` scenarios were written after seeing the held-out failures, to test this idea, and they lean towards the cases tools-v2 targets.
- tools-v1's last scenario (3 conversations) came from a rerun after Docker stopped; the other 33 are from the original run.

## Reproduce

```bash
make db-up && make migrate
uv run python -m evals.runner --split exp --assistant workflow
TOOLSET=tools-v1 uv run python -m evals.runner --split exp --assistant agent
TOOLSET=tools-v2 uv run python -m evals.runner --split exp --assistant agent
```
