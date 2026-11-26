"""The model client, and the log of every model call.

OllamaLLM talks to a local Ollama server. FakeLLM returns scripted replies so
the assistants, runner and grader can be tested without a model (CI has none).

Benchmark finding (26 Nov 2026): at temperature 0 with a system prompt,
qwen3.5:4b repeatedly writes a malformed tool call and Ollama answers HTTP 500
("XML syntax error"). A fixed seed at temperature 0.2 does the same, and a
retry with the same seed repeats it exactly. So a malformed call is retried
once with the next seed, and otherwise returned as an error that gets counted.
"""

import copy
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import text

from app.config import settings
from app.services.context import Ctx


@dataclass
class LLMReply:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)   # [{"name": ..., "arguments": {...}}]
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: int = 0
    error: str | None = None          # malformed_tool_call | timeout | unavailable | http_<code>


class LLM(Protocol):
    name: str

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             schema: dict | None = None) -> LLMReply: ...


Transport = Callable[[str, dict, int], dict]


def _http_post(url: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


# Workaround (measured 26 Nov 2026): qwen3.5:4b writes a malformed call for a tool
# with no parameters (Ollama: "element <function> closed by </parameter>"), in
# about 8 of 10 tries. Giving such a tool one optional field fixed it in 10 of 10.
# The field is added only here and removed from the reply, so the toolset itself
# stays model-neutral.
NOTE_FIELD = "note"


def _with_note_if_empty(schema: dict) -> dict:
    if schema.get("properties"):
        return schema
    return {"type": "object", "required": [], "properties": {NOTE_FIELD: {
        "type": "string", "description": "Optional. Leave empty."}}}


def _without_note(arguments: dict | None) -> dict:
    args = dict(arguments or {})
    args.pop(NOTE_FIELD, None)
    return args


def _is_malformed_tool_call(detail: str) -> bool:
    return "syntax error" in detail.lower() or "parse" in detail.lower()


class OllamaLLM:
    def __init__(self, model: str = settings.llm_model, digest: str = settings.llm_digest,
                 url: str = settings.ollama_url, transport: Transport = _http_post,
                 num_ctx: int = settings.llm_num_ctx):
        self.model, self.digest, self.url, self.num_ctx = model, digest, url, num_ctx
        self.name = f"{model}@{digest}"
        self._post = transport   # injectable for tests

    def verify(self) -> None:
        """Fail early if the installed model is not the pinned build."""
        with urllib.request.urlopen(f"{self.url}/api/tags", timeout=10) as resp:
            tags = json.load(resp)["models"]
        found = {m["name"]: m["digest"] for m in tags}
        if self.model not in found:
            raise RuntimeError(f"model {self.model} is not installed in Ollama")
        if not found[self.model].startswith(self.digest):
            raise RuntimeError(f"{self.model} is {found[self.model][:12]}, expected {self.digest}")

    def chat(self, messages, tools=None, schema=None) -> LLMReply:
        body = {
            "model": self.model, "messages": messages, "stream": False, "think": False,
            "options": {"temperature": settings.llm_temperature, "seed": settings.llm_seed,
                        "num_ctx": self.num_ctx},
        }
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": _with_note_if_empty(t["input_schema"])}} for t in tools]
        if schema:
            body["format"] = schema   # constrained decoding to this JSON schema

        started = time.perf_counter()
        error = None
        for attempt in (1, 2):
            # A retry needs a different seed, or it reproduces the same malformed call.
            # Still fixed per attempt, so runs stay reproducible.
            body["options"]["seed"] = settings.llm_seed + attempt - 1
            try:
                r = self._post(f"{self.url}/api/chat", body, settings.llm_timeout_s)
                msg = r.get("message", {})
                return LLMReply(
                    content=msg.get("content", ""),
                    tool_calls=[{"name": c["function"]["name"],
                                 "arguments": _without_note(c["function"].get("arguments"))}
                                for c in msg.get("tool_calls") or []],
                    prompt_tokens=r.get("prompt_eval_count"),
                    output_tokens=r.get("eval_count"),
                    duration_ms=round((time.perf_counter() - started) * 1000),
                )
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace") if e.fp else ""
                if e.code == 500 and _is_malformed_tool_call(detail):
                    error = "malformed_tool_call"
                    continue   # one retry; sampling may produce a well-formed call
                error = f"http_{e.code}"
            except TimeoutError:
                error = "timeout"
            except urllib.error.URLError as e:
                error = "timeout" if isinstance(e.reason, TimeoutError) else "unavailable"
            break
        return LLMReply(error=error, duration_ms=round((time.perf_counter() - started) * 1000))


class FakeLLM:
    """Returns scripted replies in order. Each item is an LLMReply, or a dict
    (JSON content for schema calls), or a str (plain content)."""

    name = "fake@test"

    def __init__(self, replies: list):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def chat(self, messages, tools=None, schema=None) -> LLMReply:
        # A copy: callers keep appending to the same history list.
        self.calls.append({"messages": copy.deepcopy(messages), "tools": tools,
                           "schema": schema})
        if not self.replies:
            raise AssertionError("FakeLLM ran out of scripted replies")
        r = self.replies.pop(0)
        if isinstance(r, LLMReply):
            return r
        if isinstance(r, dict):
            return LLMReply(content=json.dumps(r), prompt_tokens=10, output_tokens=5)
        return LLMReply(content=r, prompt_tokens=10, output_tokens=5)


def record_call(ctx: Ctx, llm: LLM, turn: int, purpose: str, reply: LLMReply) -> None:
    with ctx.engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO model_calls (session_id, trace_id, turn, purpose, model,
                                         prompt_tokens, output_tokens, duration_ms, error)
                VALUES (:s, :t, :turn, :p, :m, :pt, :ot, :ms, :e)
            """),
            {"s": ctx.session_id, "t": ctx.trace_id, "turn": turn, "p": purpose,
             "m": llm.name, "pt": reply.prompt_tokens, "ot": reply.output_tokens,
             "ms": reply.duration_ms, "e": reply.error},
        )
