"""The provider adapters translate the neutral conversation correctly."""

from types import SimpleNamespace

import pytest

from autopilot.doctor import llm as llm_mod
from autopilot.doctor.llm import AnthropicLLM, BedrockLLM
from autopilot.doctor.tools import Tool

TOOLS = [Tool("trace_errors", "desc", {"properties": {"service": {"type": "string"}}}, None)]
CONVERSATION = [
    {"role": "user", "content": [{"type": "text", "text": "find it"}]},
    {"role": "assistant", "content": [
        {"type": "text", "text": "checking"},
        {"type": "tool_call", "id": "t1", "name": "trace_errors", "input": {"service": "payments"}}]},
    {"role": "user", "content": [
        {"type": "tool_result", "id": "t1", "content": '{"ok": 1}', "is_error": False}]},
]


class FakeAnthropic:
    def __init__(self):
        self.messages = self
        self.request = None

    def create(self, **kw):
        self.request = kw
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="next"),
                     SimpleNamespace(type="tool_use", id="t2", name="trace_errors", input={})],
            stop_reason="tool_use", usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def test_anthropic_translation():
    client = FakeAnthropic()
    turn = AnthropicLLM("model-x", client=client).turn("sys", CONVERSATION, TOOLS)
    req = client.request
    assert req["model"] == "model-x" and req["system"] == "sys"
    assert req["tools"][0]["input_schema"]["properties"] == {"service": {"type": "string"}}
    assert req["messages"][1]["content"][1] == {
        "type": "tool_use", "id": "t1", "name": "trace_errors", "input": {"service": "payments"}}
    assert req["messages"][2]["content"][0]["tool_use_id"] == "t1"
    assert turn.text == "next" and turn.tool_calls[0].id == "t2"
    assert turn.usage == {"input_tokens": 10, "output_tokens": 5}


class FakeBedrock:
    def __init__(self):
        self.request = None

    def converse(self, **kw):
        self.request = kw
        return {"output": {"message": {"content": [
            {"text": "next"},
            {"toolUse": {"toolUseId": "t2", "name": "trace_errors", "input": {"service": "x"}}}]}},
            "stopReason": "tool_use", "usage": {"inputTokens": 7, "outputTokens": 3}}


def test_bedrock_translation():
    client = FakeBedrock()
    turn = BedrockLLM("model-y", client=client).turn("sys", CONVERSATION, TOOLS)
    req = client.request
    assert req["modelId"] == "model-y" and req["system"] == [{"text": "sys"}]
    assert req["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]["type"] == "object"
    assert req["messages"][1]["content"][1] == {
        "toolUse": {"toolUseId": "t1", "name": "trace_errors", "input": {"service": "payments"}}}
    assert req["messages"][2]["content"][0]["toolResult"] == {
        "toolUseId": "t1", "content": [{"text": '{"ok": 1}'}], "status": "success"}
    assert turn.tool_calls[0].input == {"service": "x"}
    assert turn.usage == {"input_tokens": 7, "output_tokens": 3}


def test_backend_selection(monkeypatch):
    monkeypatch.delenv("AUTOPILOT_LLM", raising=False)
    assert llm_mod.from_env() is None
    monkeypatch.setenv("AUTOPILOT_LLM", "bedrock")
    monkeypatch.delenv("AUTOPILOT_LLM_MODEL", raising=False)
    with pytest.raises(ValueError, match="AUTOPILOT_LLM_MODEL"):
        llm_mod.from_env()
    monkeypatch.setenv("AUTOPILOT_LLM", "carrier-pigeon")
    monkeypatch.setenv("AUTOPILOT_LLM_MODEL", "m")
    with pytest.raises(ValueError, match="unknown"):
        llm_mod.from_env()
