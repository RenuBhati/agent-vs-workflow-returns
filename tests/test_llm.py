"""Model client: request shape, retry on malformed tool calls, errors as data.

Uses a fake transport, so no Ollama is needed. The one live test is skipped
unless RUN_OLLAMA=1.
"""

import copy
import io
import os
import urllib.error

import pytest

from app.llm import FakeLLM, LLMReply, OllamaLLM


def http_error(code, body):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body.encode()))


OK = {"message": {"content": "", "tool_calls": [
    {"function": {"name": "list_orders", "arguments": {}}}]},
    "prompt_eval_count": 1100, "eval_count": 12}


def scripted(*outcomes):
    """A transport that returns or raises each outcome in turn, recording requests."""
    sent = []

    def post(url, body, timeout):
        sent.append(copy.deepcopy(body))   # as sent at that moment
        o = outcomes[len(sent) - 1]
        if isinstance(o, Exception):
            raise o
        return o
    return post, sent


def test_request_shape_pins_settings_and_converts_tools():
    post, sent = scripted(OK)
    schema = {"type": "object", "properties": {"order_unit_id": {"type": "string"}}}
    tools = [{"name": "get_action_status", "description": "d", "input_schema": schema}]
    reply = OllamaLLM(transport=post).chat([{"role": "user", "content": "hi"}], tools=tools)
    body = sent[0]
    assert body["think"] is False and body["stream"] is False
    assert body["options"]["temperature"] > 0 and "seed" in body["options"]
    assert body["tools"][0]["function"]["parameters"] == schema
    assert reply.tool_calls == [{"name": "list_orders", "arguments": {}}]
    assert (reply.prompt_tokens, reply.output_tokens, reply.error) == (1100, 12, None)


def test_schema_is_sent_as_format():
    post, sent = scripted({"message": {"content": "{}"}})
    OllamaLLM(transport=post).chat([], schema={"type": "object"})
    assert sent[0]["format"] == {"type": "object"}


def test_malformed_tool_call_is_retried_once_then_succeeds():
    bad = http_error(500, '{"error":"XML syntax error on line 3"}')
    post, sent = scripted(bad, OK)
    reply = OllamaLLM(transport=post).chat([])
    assert len(sent) == 2 and reply.error is None and reply.tool_calls
    # The retry must use a different seed, or it would repeat the same output.
    assert sent[1]["options"]["seed"] == sent[0]["options"]["seed"] + 1


def test_malformed_twice_is_returned_as_an_error_not_raised():
    def bad():  # a fresh error each time, as Ollama sends
        return http_error(500, '{"error":"XML syntax error on line 3"}')
    post, sent = scripted(bad(), bad())
    reply = OllamaLLM(transport=post).chat([])
    assert len(sent) == 2 and reply.error == "malformed_tool_call" and not reply.tool_calls


def test_other_http_errors_are_not_retried():
    post, sent = scripted(http_error(500, '{"error":"out of memory"}'))
    assert OllamaLLM(transport=post).chat([]).error == "http_500"
    assert len(sent) == 1


def test_server_down_and_timeout_are_errors():
    post, _ = scripted(urllib.error.URLError(ConnectionRefusedError()))
    assert OllamaLLM(transport=post).chat([]).error == "unavailable"
    post, _ = scripted(TimeoutError())
    assert OllamaLLM(transport=post).chat([]).error == "timeout"


def test_fake_llm_scripts_replies():
    fake = FakeLLM([{"intent": "return"}, "hello", LLMReply(error="timeout")])
    assert fake.chat([]).content == '{"intent": "return"}'
    assert fake.chat([]).content == "hello"
    assert fake.chat([]).error == "timeout"
    with pytest.raises(AssertionError, match="ran out"):
        fake.chat([])


@pytest.mark.skipif(os.environ.get("RUN_OLLAMA") != "1", reason="needs local Ollama")
def test_live_ollama_pinned_model_calls_a_tool():
    from app.tools import tool_schemas
    llm = OllamaLLM()
    llm.verify()
    reply = llm.chat([{"role": "system", "content": "You are a returns assistant. Use tools."},
                      {"role": "user", "content": "Exchange the blue one for a medium"}],
                     tools=tool_schemas())
    assert reply.error is None, reply
    assert reply.tool_calls and reply.tool_calls[0]["name"] == "list_orders"


def test_no_argument_tools_get_an_optional_note_that_is_stripped():
    # Workaround for qwen3.5:4b's malformed calls to parameterless tools.
    reply_with_note = {"message": {"tool_calls": [
        {"function": {"name": "list_orders", "arguments": {"note": "find the blue item"}}}]}}
    post, sent = scripted(reply_with_note)
    tools = [{"name": "list_orders", "description": "d",
              "input_schema": {"type": "object", "properties": {}}},
             {"name": "get_action_status", "description": "d",
              "input_schema": {"type": "object", "properties": {"order_unit_id": {"type": "string"}}}}]
    reply = OllamaLLM(transport=post).chat([], tools=tools)
    sent_params = [t["function"]["parameters"] for t in sent[0]["tools"]]
    assert "note" in sent_params[0]["properties"]          # added to the empty one
    assert "note" not in sent_params[1]["properties"]      # left alone otherwise
    assert reply.tool_calls == [{"name": "list_orders", "arguments": {}}]   # stripped
