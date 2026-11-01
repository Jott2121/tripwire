"""Provider-neutral model clients for the v2 study (no network in tests, no environment reads).

The harness speaks one shape, :class:`~tripwire.xai.ChatRequest` in and
:class:`~tripwire.xai.ChatResponse` out, through :data:`ModelClient` (the same protocol as
``xai.XaiClient``, kept under its old name for v1). This module adds the two clients stage 1 and
stage 2 need beyond xAI:

* :class:`ChatCompletionsClient`: an OpenAI-compatible ``/v1/chat/completions`` client, used for
  ``openai/gpt-oss-120b`` on Together (stage 1).
* :class:`AnthropicClient`: the Claude Messages API, used for ``claude-sonnet-5-5`` (stage 2).

The Gemini row is priced and registered but has no client yet: :func:`make_client` refuses it
until its wire format is confirmed from Google's own docs (stage 2, before 2026-12-31).

Every client is raw HTTP over the same injectable :data:`~tripwire.xai.Transport` as v1, so tests
use recorded fakes and the repo stays stdlib-first (no vendor SDK dependency). Parsing is strict:
an unknown block, stop reason or usage shape raises, and cost is never guessed. API keys are
passed in by the caller; this module never reads the environment and never prints a key.

Wire formats were read from the vendors' pages on 2026-11-01 (sources next to each client). The
20-run pilot per new model (preregistration v2, "Pilots") is where they are confirmed live,
before any scored run.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from tripwire.xai import (
    GROK_4_7_PRICING,
    ChatRequest,
    ChatResponse,
    LiveCallsDisabled,
    LiveClient,
    ModelConfig,
    Pricing,
    QuotaExhausted,
    ToolCallRequest,
    Transport,
    UsageUnrecognised,
    XaiClient,
    XaiError,
    urllib_transport,
)

ModelClient = XaiClient  # provider-neutral name; v1 code keeps importing XaiClient
ModelError = XaiError

MAX_OUTPUT_TOKENS = 16_000  # per request; a reply that hits it is truncated and raises
HALT_STATUSES = frozenset({402, 429, 529})  # out of credit, rate/quota, overloaded: halt, resume


def _post(
    transport: Transport, url: str, headers: Mapping[str, str], body: Mapping[str, Any], t: float
) -> dict[str, Any]:
    status, raw = transport(url, headers, json.dumps(body).encode(), t)
    if status in HALT_STATUSES:
        raise QuotaExhausted(f"HTTP {status}")
    if status != 200:
        raise XaiError(f"HTTP {status}")  # the body is not echoed: it could carry anything
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise XaiError("response is not JSON") from exc
    if not isinstance(parsed, dict):
        raise XaiError("response is not a JSON object")
    return parsed


def _count(usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UsageUnrecognised(f"usage.{key} missing or not a non-negative integer")
    return value


def _args(raw: Any) -> dict[str, Any]:
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError as exc:
        raise XaiError("tool call arguments are not JSON") from exc
    if not isinstance(args, dict):
        raise XaiError("tool call arguments are not an object")
    return args


class _Keyed:
    def __init__(self, api_key: str, transport: Transport, timeout_s: float) -> None:
        if not api_key:
            raise LiveCallsDisabled("no API key supplied")
        self._key, self._transport, self._timeout = api_key, transport, timeout_s

    def __repr__(self) -> str:  # never show the key
        return f"{type(self).__name__}(<redacted>)"


# --- OpenAI-compatible chat completions (Together) ----------------------------------------------
# Read 2026-11-01 from https://docs.together.ai/reference/chat-completions-1: POST
# /v1/chat/completions with a Bearer key; tools are {"type": "function", "function": {...}};
# assistant messages may carry ``reasoning`` (echoed back unchanged here); tool results are
# {"role": "tool", "tool_call_id", "content"}; ``reasoning_effort`` is low | medium | high;
# usage has prompt_tokens / completion_tokens and no cached-token field; finish_reason is one of
# stop, eos, length, tool_calls, function_call.
TOGETHER_URL = "https://api.together.ai/v1/chat/completions"
_FINAL = frozenset({"stop", "eos", "tool_calls"})


def _cc_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            items = m.get("output_items")
            if items:  # the provider's own message, verbatim (keeps gpt-oss reasoning)
                out.append({k: v for k, v in dict(items[0]).items() if v is not None})
            else:
                out.append({"role": "assistant", "content": m.get("content") or "",
                            "tool_calls": list(m.get("tool_calls", []))})  # fmt: skip
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        else:
            raise XaiError(f"unsupported message role {role!r}")
    return out


def parse_chat_completion(body: Mapping[str, Any]) -> ChatResponse:
    """Strict parse of one chat completion; anything unexpected raises."""
    if body.get("error"):
        raise XaiError("API returned an error object")
    served, choices, usage = body.get("model"), body.get("choices"), body.get("usage")
    if not isinstance(served, str) or not served or not isinstance(choices, list) or not choices:
        raise XaiError("response lacks a model id or choices")
    if not isinstance(usage, dict):
        raise UsageUnrecognised("response has no usage object")
    n_in, n_out = _count(usage, "prompt_tokens"), _count(usage, "completion_tokens")
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(message, dict):
        raise XaiError("choice has no message")
    finish = choice.get("finish_reason")
    if finish == "length":
        raise XaiError("reply truncated at the output token limit")
    if finish not in _FINAL:
        raise XaiError(f"unsupported finish_reason {finish!r}")
    calls: list[ToolCallRequest] = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") if isinstance(tc, dict) else None
        if not isinstance(fn, dict) or tc.get("type", "function") != "function":
            raise XaiError("malformed tool call")
        try:
            calls.append(ToolCallRequest(tc["id"], fn["name"], _args(fn["arguments"])))
        except KeyError as exc:
            raise XaiError("malformed tool call") from exc
    text = message.get("content") or ""
    if not isinstance(text, str):
        raise XaiError("message content is not a string")
    return ChatResponse(served, text, n_in, n_out, tuple(calls), output_items=(dict(message),))


class ChatCompletionsClient(_Keyed):
    """OpenAI-compatible client (Together). HTTP 402/429/529 halt the runner (resumable)."""

    def __init__(
        self,
        api_key: str,
        transport: Transport = urllib_transport,
        timeout_s: float = 600.0,
        url: str = TOGETHER_URL,
    ) -> None:
        super().__init__(api_key, transport, timeout_s)
        self._url = url

    def chat(self, request: ChatRequest) -> ChatResponse:
        if request.remote_mcp is not None:
            raise LiveCallsDisabled("server-side remote MCP is xAI only")
        body: dict[str, Any] = {
            "model": request.model,
            "messages": _cc_messages(request.messages),
            "tools": [dict(t) for t in request.tools],
            "max_tokens": MAX_OUTPUT_TOKENS,
        }
        if request.effort is not None:
            body["reasoning_effort"] = request.effort
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        raw = _post(self._transport, self._url, headers, body, self._timeout)
        return parse_chat_completion(raw)


# --- Anthropic Messages ------------------------------------------------------------------------
# Read 2026-11-01 from Anthropic's API docs (Messages, tool use, prompt caching, effort):
# POST https://api.anthropic.com/v1/messages with x-api-key and anthropic-version; tools are
# {"name", "description", "input_schema"}; the reply's content blocks (text, thinking,
# redacted_thinking, tool_use) are echoed back unchanged as the next assistant turn, so thinking
# stays valid; every tool result goes in ONE user message of tool_result blocks; effort is
# output_config.effort; top-level cache_control {"type": "ephemeral"} caches the growing prefix;
# usage.input_tokens is the uncached remainder, plus cache_creation_input_tokens and
# cache_read_input_tokens. No server-side refusal fallback is sent: it would answer with another
# model, and the study scores the model it asked for. A refusal is kept as the final reply.
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2026-10-21"
_BLOCKS = frozenset({"text", "thinking", "redacted_thinking", "tool_use"})


def _anthropic_tool(spec: Mapping[str, Any]) -> dict[str, Any]:
    fn = spec["function"]
    return {"name": fn["name"], "description": fn["description"], "input_schema": fn["parameters"]}


def _anthropic_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            if not m.get("output_items"):
                raise XaiError("assistant turn has no provider content blocks to echo back")
            out.append({"role": "assistant", "content": [dict(b) for b in m["output_items"]]})
        elif role == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"],
                     "content": m["content"]}  # fmt: skip
            last = out[-1] if out else None
            if last and last["role"] == "user" and isinstance(last["content"], list):
                last["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        else:
            raise XaiError(f"unsupported message role {role!r}")
    return out


def parse_anthropic(body: Mapping[str, Any]) -> ChatResponse:
    """Strict parse of one Messages API reply; anything unexpected raises."""
    if body.get("type") == "error" or body.get("error"):
        raise XaiError("API returned an error object")
    served, content, usage = body.get("model"), body.get("content"), body.get("usage")
    if not isinstance(served, str) or not served or not isinstance(content, list):
        raise XaiError("response lacks a model id or content list")
    if not isinstance(usage, dict):
        raise UsageUnrecognised("response has no usage object")
    uncached, n_out = _count(usage, "input_tokens"), _count(usage, "output_tokens")
    written = _count(usage, "cache_creation_input_tokens")
    read = _count(usage, "cache_read_input_tokens")
    stop = body.get("stop_reason")
    if stop == "max_tokens":
        raise XaiError("reply truncated at the output token limit")
    if stop not in ("end_turn", "tool_use", "refusal"):
        raise XaiError(f"unsupported stop_reason {stop!r}")
    text: list[str] = []
    calls: list[ToolCallRequest] = []
    for block in content:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind not in _BLOCKS:
            raise XaiError(f"unknown content block {kind!r}")
        if kind == "text":
            text.append(str(block["text"]))
        elif kind == "tool_use":
            try:
                calls.append(ToolCallRequest(block["id"], block["name"], _args(block["input"])))
            except KeyError as exc:
                raise XaiError("malformed tool_use block") from exc
    refused = stop == "refusal"
    if stop == "tool_use" and not calls:
        raise XaiError("stop_reason tool_use without a tool_use block")
    return ChatResponse(
        served,
        "".join(text),
        uncached + written + read,
        n_out,
        () if refused else tuple(calls),  # a refused turn ends the run; nothing is executed
        output_items=tuple(dict(b) for b in content),
        cached_input_tokens=read,
        cache_write_tokens=written,
        refused=refused,
    )


class AnthropicClient(_Keyed):
    """Claude Messages API client. HTTP 402/429/529 halt the runner (resumable)."""

    def __init__(
        self, api_key: str, transport: Transport = urllib_transport, timeout_s: float = 600.0
    ) -> None:
        super().__init__(api_key, transport, timeout_s)

    def chat(self, request: ChatRequest) -> ChatResponse:
        if request.remote_mcp is not None:
            raise LiveCallsDisabled("server-side remote MCP is xAI only")
        body: dict[str, Any] = {
            "model": request.model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "messages": _anthropic_messages(request.messages),
            "tools": [_anthropic_tool(t) for t in request.tools],
            "cache_control": {"type": "ephemeral"},
        }
        if request.effort is not None:
            body["output_config"] = {"effort": request.effort}
        headers = {
            "x-api-key": self._key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        return parse_anthropic(_post(self._transport, ANTHROPIC_URL, headers, body, self._timeout))


# --- the v2 model rows (preregistration v2, "Models") ------------------------------------------
SONNET_5_5_PRICING = Pricing(
    2.00,
    10.00,
    "https://platform.claude.com/docs/en/about-claude/pricing (retrieved 2026-11-01)",
    cached_input_per_mtok=0.10,
    cache_write_per_mtok=2.50,  # 5-minute writes, the default TTL used here
)
GEMINI_3_8_FLASH_PRICING = Pricing(
    0.75,
    3.75,
    "https://ai.google.dev/gemini-api/docs/pricing (retrieved 2026-11-01)",
    cached_input_per_mtok=0.075,
    valid_until=date(2026, 12, 31),  # the page says these rates double from 2027-01-01
)
GPT_OSS_120B_PRICING = Pricing(
    0.15, 0.60, "https://docs.together.ai/docs/serverless/models (retrieved 2026-11-01)"
)


@dataclass(frozen=True)
class ModelRow:
    """One v2 model: its config, provider, price and the env var its key lives in."""

    slot: str
    stage: int
    provider: str  # xai | together | anthropic | google
    config: ModelConfig
    pricing: Pricing
    key_env: str


V2_MODELS: tuple[ModelRow, ...] = (
    ModelRow("M1", 1, "xai", ModelConfig("grok-4.7", "grok-4.7", "medium", "frontier"),
             GROK_4_7_PRICING, "XAI_API_KEY"),
    ModelRow("M4", 1, "together", ModelConfig("gpt-oss-120b", "openai/gpt-oss-120b", "medium",
             "open-weight"), GPT_OSS_120B_PRICING, "TOGETHER_API_KEY"),
    ModelRow("M2", 2, "anthropic", ModelConfig("claude-sonnet-5-5", "claude-sonnet-5-5",
             "medium", "frontier"), SONNET_5_5_PRICING, "ANTHROPIC_API_KEY"),
    ModelRow("M3", 2, "google", ModelConfig("gemini-3.8-flash", "gemini-3.8-flash", "medium",
             "small"), GEMINI_3_8_FLASH_PRICING, "GEMINI_API_KEY"),
)  # fmt: skip


def v2_row(label: str) -> ModelRow:
    for row in V2_MODELS:
        if row.config.label == label:
            return row
    raise KeyError(f"no v2 model row {label!r}")


def make_client(
    row: ModelRow, api_key: str, transport: Transport = urllib_transport
) -> ModelClient:
    """The live client for a row. Gemini is refused until its client is built and confirmed."""
    if row.provider == "xai":
        return LiveClient(api_key, transport)
    if row.provider == "together":
        return ChatCompletionsClient(api_key, transport)
    if row.provider == "anthropic":
        return AnthropicClient(api_key, transport)
    raise LiveCallsDisabled(f"no confirmed client for provider {row.provider!r} yet")
