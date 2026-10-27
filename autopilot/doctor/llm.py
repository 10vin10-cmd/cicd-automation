"""Pluggable LLM backends for the investigating agents.

The agent loop speaks one neutral message format; each adapter translates it
to its provider's tool-use API and back:

    {"role": "user" | "assistant", "content": [block, ...]}
    block = {"type": "text", "text": ...}
          | {"type": "tool_call", "id": ..., "name": ..., "input": {...}}
          | {"type": "tool_result", "id": ..., "content": "<json>", "is_error": bool}

Backends are chosen with AUTOPILOT_LLM (bedrock | anthropic) and
AUTOPILOT_LLM_MODEL (the provider's model ID). There is deliberately no
default model ID: model names change, so you choose one explicitly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

from autopilot.doctor.tools import Tool


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Turn:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    usage: dict = field(default_factory=dict)


class LLM(Protocol):
    name: str

    def turn(self, system: str, messages: list[dict], tools: list[Tool]) -> Turn: ...


class AnthropicLLM:
    """Anthropic Messages API with tool use."""
    name = "anthropic"

    def __init__(self, model: str, client=None, max_tokens: int = 2048):
        if client is None:
            import anthropic  # optional dependency
            client = anthropic.Anthropic()
        self.client, self.model, self.max_tokens = client, model, max_tokens

    def turn(self, system, messages, tools):
        resp = self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system,
            tools=[{"name": t.name, "description": t.description, "input_schema": t.schema()}
                   for t in tools],
            messages=[{"role": m["role"], "content": [self._block(b) for b in m["content"]]}
                      for m in messages],
        )
        text, calls = [], []
        for block in resp.content:
            if block.type == "text":
                text.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(block.id, block.name, dict(block.input or {})))
        usage = getattr(resp, "usage", None)
        return Turn("\n".join(text), calls, resp.stop_reason or "",
                    {"input_tokens": getattr(usage, "input_tokens", 0),
                     "output_tokens": getattr(usage, "output_tokens", 0)} if usage else {})

    @staticmethod
    def _block(b: dict) -> dict:
        if b["type"] == "text":
            return {"type": "text", "text": b["text"]}
        if b["type"] == "tool_call":
            return {"type": "tool_use", "id": b["id"], "name": b["name"], "input": b["input"]}
        return {"type": "tool_result", "tool_use_id": b["id"], "content": b["content"],
                "is_error": b.get("is_error", False)}


class BedrockLLM:
    """Amazon Bedrock Converse API with tool use (any Converse model that
    supports tools)."""
    name = "bedrock"

    def __init__(self, model: str, client=None, max_tokens: int = 2048):
        if client is None:
            import boto3
            client = boto3.client("bedrock-runtime")
        self.client, self.model, self.max_tokens = client, model, max_tokens

    def turn(self, system, messages, tools):
        resp = self.client.converse(
            modelId=self.model,
            system=[{"text": system}],
            messages=[{"role": m["role"], "content": [self._block(b) for b in m["content"]]}
                      for m in messages],
            toolConfig={"tools": [{"toolSpec": {"name": t.name, "description": t.description,
                                                "inputSchema": {"json": t.schema()}}}
                                  for t in tools]},
            inferenceConfig={"maxTokens": self.max_tokens, "temperature": 0},
        )
        text, calls = [], []
        for block in resp["output"]["message"]["content"]:
            if "text" in block:
                text.append(block["text"])
            elif "toolUse" in block:
                tu = block["toolUse"]
                calls.append(ToolCall(tu["toolUseId"], tu["name"], dict(tu.get("input") or {})))
        usage = resp.get("usage", {})
        return Turn("\n".join(text), calls, resp.get("stopReason", ""),
                    {"input_tokens": usage.get("inputTokens", 0),
                     "output_tokens": usage.get("outputTokens", 0)})

    @staticmethod
    def _block(b: dict) -> dict:
        if b["type"] == "text":
            return {"text": b["text"]}
        if b["type"] == "tool_call":
            return {"toolUse": {"toolUseId": b["id"], "name": b["name"], "input": b["input"]}}
        return {"toolResult": {"toolUseId": b["id"], "content": [{"text": b["content"]}],
                               "status": "error" if b.get("is_error") else "success"}}


def from_env() -> LLM | None:
    """The configured backend, or None for the offline playbook."""
    backend = os.environ.get("AUTOPILOT_LLM", "").strip().lower()
    if backend in ("", "none", "playbook", "offline"):
        return None
    model = os.environ.get("AUTOPILOT_LLM_MODEL", "").strip()
    if not model:
        raise ValueError("AUTOPILOT_LLM_MODEL must name the model to use")
    if backend == "bedrock":
        return BedrockLLM(model)
    if backend == "anthropic":
        return AnthropicLLM(model)
    raise ValueError(f"unknown AUTOPILOT_LLM backend: {backend}")
