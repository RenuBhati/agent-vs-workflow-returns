"""Assemble slides + recorded demo scenes + Kokoro narration into one MP4.

PLAN lists segments in order: ("slide", id) shows slides/<id>.jpg, ("scene", name) plays
the recorded frames for that scene from rec/. Model waits inside scenes are squeezed to
WAIT_S seconds (the recording shows a running real-time timer). Every segment lasts at
least as long as its narration; a scene holds its last frame if the narration is longer.

    .venv/bin/python build.py demo.mp4   (see demo/README.md)
"""

import json
import re
import subprocess
import sys

import imageio_ffmpeg
import numpy as np
import soundfile as sf
from kokoro_onnx import Kokoro

WAIT_S = 2.5
LEAD_S, TAIL_S = 0.5, 0.9
VOICE, SPEED, SR = "af_heart", 1.0, 24000
OUT = sys.argv[1] if len(sys.argv) > 1 else "demo.mp4"

with open("rec/frames.json") as fh:
    rec = json.load(fh)
with open("rec/beats.json") as fh:
    meta = json.load(fh)
frames, waits = rec["frames"], meta["waits"]
beat = {b["name"]: b for b in meta["beats"]}


def totals(s):
    m = re.search(r"(\d+) model calls? · ([\d,]+) prompt tokens", s)
    return int(m.group(1)), int(m.group(2).replace(",", ""))


def about(n):
    return f"{round(n, -2):,}" if n < 1000 else f"{round(n, -3):,}"


wc, wt = totals(beat["workflow_confirm"]["totals"])
ac, at = totals(beat["agent_confirm"]["totals"])
first = beat["agent_ask"]["seconds"]
first_said = f"about {round(first / 60)} minutes" if first >= 90 else f"{round(first)} seconds"

PLAN = [
    ("slide", "s-title",
     ("This is a technical walkthrough of a small research project. The question: for clothing "
     "returns and exchanges, does a tool-using agent beat a fixed workflow, and what does it "
     "cost? I'll show how both are built, run them live, and share what I learned.")),
    ("slide", "s-stack",
     ("The stack is deliberately plain. Python and FastAPI, with Pydantic for every input and "
     "output. Postgres sixteen, through SQLAlchemy Core and plain SQL migrations. The model is "
     "Qwen three point five, four billion parameters, running locally through Ollama on an "
     "eight gigabyte MacBook Air. There's no agent framework, so every step is in my own code. "
     "About two hundred tests run in GitHub Actions on every push.")),
    ("slide", "s-arch",
     ("The demo page and the evaluation runner both talk to the same FastAPI "
     "app. Behind it sit the two assistants. Both call the same six tools, and the tools call "
     "the services, which are the only code that writes. Postgres constraints enforce the "
     "critical rules themselves. So the two assistants differ in exactly one thing: who "
     "chooses the next step.")),
    ("slide", "s-workflow",
     ("In the workflow, the model has one job: read the message. It fills this small schema: "
     "the intent, which item numbers, the size, a yes or no, and whether they want a person. "
     "Ollama's structured output takes the JSON schema, so the reply always parses. Then a "
     "plain Python state machine decides what happens: pick the item, ask a question, check "
     "the policy and the stock, or propose. The model never chooses a tool.")),
    ("slide", "s-agent",
     ("In the agent, the model is in charge. Each turn it gets up to eight steps. Every step "
     "sends the system prompt, seven tool schemas and the whole history, which is why it costs "
     "so many tokens. It ends a turn by calling respond to shopper, which tags the reply with "
     "the kind of question it asks. Those are the same tags the workflow uses, so both are "
     "graded the same way. The history lives in the database, so the server stays stateless.")),
    ("scene", "workflow_ask",
     ("Let's run them. First, the workflow. Amira wants to swap her blue Oxford shirt for a "
     "medium. One model call reads her message, and the code does the rest: it checks the "
     "policy and the stock, and shows a proposal.")),
    ("scene", "workflow_confirm",
     ("Nothing changes until she presses Confirm. Then the exchange is booked, and she gets a "
     f"reference. That took {wc} model call{'s' if wc != 1 else ''} and about {about(wt)} "
     "prompt tokens.")),
    ("scene", "agent_ask",
     ("Now the agent, with the same request. Here the model decides which tools to call, one "
     f"step at a time. On this laptop its first reply took {first_said}, so the wait is sped "
     "up. On the right you can see each tool it called.")),
    ("scene", "agent_confirm",
     (f"It gets the same result. But it needed {ac} model calls and about {about(at)} prompt "
     f"tokens, against {wc} and {about(wt)} for the workflow.")),
    ("slide", "s-safety",
     ("Now the safety design, because the model can propose anything, but it can't commit it. "
     "Each proposal is a versioned row, and the Confirm button records the shopper's click on "
     "that exact version. Commit checks the policy and stock again. Stock is taken with a "
     "conditional update, so the check and the change happen in one statement, and a partial "
     "unique index allows one active action per item. I tested the races with real threads: "
     "in twenty five rounds of two shoppers racing for the last shirt, there was exactly one "
     "winner every time.")),
    ("scene", "return_setup",
     "Here's what that looks like. Amira asks to return her white Oxford, and gets a proposal."),
    ("scene", "yes_not_confirm",
     ("Instead of pressing Confirm, she types yes. That isn't consent. The proposal stays "
     "open, nothing is committed, and she's asked to press the button.")),
    ("slide", "s-evals",
     ("To compare the two fairly, I built an evaluation harness. Thirty scenarios, each with "
     "three wordings. A rule-based simulated shopper answers from private facts, and presses "
     "Confirm only on what it actually wants, so both assistants face exactly the same "
     "shopper. Some scenarios script a change of mind, like a correction or a cancel. The "
     "grader checks the final database state, plus things the state can't show, like a "
     "silent switch or a false claim of success. And the grader has its own tests. I tuned on "
     "thirteen dev scenarios, then froze both assistants and ran seventeen held-out scenarios "
     "once.")),
    ("slide", "s-results",
     ("On fifty-one held-out conversations each, both reached the right final state about "
     "equally often. But the workflow was cleaner, about fifteen times faster, and used about "
     "thirty-two times fewer prompt tokens. And only the agent ever claimed success when "
     "nothing had been done.")),
    ("slide", "s-exp",
     ("Then I ran one follow-up experiment. The agent's weakest spot was the policy no: final "
     "sale, past the window, already exchanged. So I changed one tool. Check options now also "
     "returns the next step, like: offer a review with this exact reason, and file it only if "
     "the shopper says yes. Same model, same prompt, and twelve new scenarios, so the held-out "
     "set stayed untouched. Clean conversations went from ten to seventeen out of thirty six, "
     "at no extra token cost. But the workflow got all thirty six. A better tool helps the "
     "agent, and the workflow still does that step every time instead of most of the time.")),
    ("slide", "s-learn1",
     ("So, what did I learn? First, a small model is a good reader but a weak planner. Given "
     "only the reading job, it was right almost every time. Asked to plan, call tools and tag "
     "questions, it drifted. Second, put safety in the services and the database, not the "
     "prompt. Under prompt injection, neither assistant committed anything wrong. Third, the "
     "database can't stop the model from lying. The agent once told a shopper their refund "
     "was done when nothing had happened. The workflow can't do that, because its success "
     "message only exists after a commit returns a reference. And fourth, code failures are "
     "cheaper than prompt failures. The workflow's misses came from two missing checks. The "
     "agent's came from about six different behaviours.")),
    ("slide", "s-learn2",
     ("A few engineering lessons too. Tools with no parameters made the model emit broken tool "
     "calls, which Ollama rejected. An optional note field fixed it. A retry with the same seed "
     "just repeats the failure, so retries change the seed. On an eight gigabyte laptop, "
     "memory is the bottleneck: Docker's virtual machine was reserving four gigabytes, and "
     "cutting it to one and a half took agent steps from up to two minutes down to about "
     "thirty-five seconds. And for evals: grade the final state, freeze before held-out, and "
     "publish every failure next to the score.")),
    ("slide", "s-close",
     ("Everything is in the repository: the code, the results, the design decisions, and a "
     "write-up of every failure. Thanks for watching.")),
]

k = Kokoro("models/kokoro-v1.0.onnx", "models/voices-v1.0.bin")
pieces, clip, audio_at = [], [], []   # (file, duration) entries; narration placements
t = 0.0
for kind, key, text in PLAN:
    s, sr = k.create(text, voice=VOICE, speed=SPEED, lang="en-us")
    assert sr == SR
    need = LEAD_S + len(s) / SR + TAIL_S
    audio_at.append((t + LEAD_S, s))
    if kind == "slide":
        clip.append((f"slides/{key}.jpg", need))
        length = need
    else:
        b = beat[key]
        a0, a1 = b["start"], b["end"]

        def squeeze(x, a0=a0):
            out = x - a0
            for w in waits:
                lo, hi = max(w["start"], a0), w["end"]
                if hi <= a0 or lo >= x:
                    continue
                span = min(hi, x) - lo
                out -= span - span * WAIT_S / (w["end"] - w["start"])
            return out

        before = [f for f in frames if f["t"] <= a0]
        inside = [f for f in frames if a0 < f["t"] <= a1]
        seq = ([(before[-1]["file"], 0.0)] if before else []) + [(f["file"], squeeze(f["t"])) for f in inside]
        video = squeeze(a1)
        length = max(video, need)
        for i, (file, at_) in enumerate(seq):
            nxt = seq[i + 1][1] if i + 1 < len(seq) else length
            if nxt - at_ > 0.0005:
                clip.append((file, nxt - at_))
    print(f"{kind:5} {key:18} {length:5.1f} s  (narration {len(s) / SR:.1f} s)")
    t += length

total = t
with open("concat.txt", "w") as fh:
    for file, d in clip:
        fh.write(f"file '{file}'\nduration {d:.4f}\n")
    fh.write(f"file '{clip[-1][0]}'\n")

track = np.zeros(int((total + 1) * SR), dtype=np.float32)
for at_, s in audio_at:
    i = int(at_ * SR)
    track[i:i + len(s)] += s
sf.write("narration.wav", track, SR)

ff = imageio_ffmpeg.get_ffmpeg_exe()
subprocess.run([ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "concat.txt",
                "-i", "narration.wav",
                "-vf", "scale=1280:800:flags=lanczos,fps=30,format=yuv420p",
                "-color_range", "tv", "-c:v", "libx264", "-preset", "slow", "-crf", "24",
                "-tune", "stillimage", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
                "-t", f"{total:.2f}", OUT], check=True)
print(f"{OUT}: {total:.1f} s ({total / 60:.1f} min)")
