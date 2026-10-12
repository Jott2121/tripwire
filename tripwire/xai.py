"""xAI client interface, model configs, and pricing (M5). No network, no environment reads.

The harness talks to xAI only through :class:`XaiClient`. Tests use :class:`ScriptedClient`;
:class:`LiveClient` (M7) is a Responses API client over an injectable transport. It refuses every
call while :data:`WIRE_CONFIRMED` is False, so a live call cannot happen by accident.
``XAI_API_KEY`` is never read here: the caller passes the key in.

The model id, effort levels and prices below were supplied by the owner in issue #25, retrieved
from docs.x.ai on 2026-10-11 (sources next to each value). Anything not in that issue stays
:data:`UNRESOLVED`, and a run against an UNRESOLVED model or price is refused by the runner.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

UNRESOLVED = "UNRESOLVED"

ToolDispatcher = Callable[[str, Mapping[str, Any]], dict[str, Any]]


class XaiError(RuntimeError):
    """The client could not complete a request (a RUN_ERROR, never a score)."""


class QuotaExhausted(XaiError):
    """The account's quota or rate budget is spent: the runner halts instead of retrying.

    ``paid_usd`` is the known cost of requests the harness already completed in the run this
    interrupted (set by ``harness.run_one`` before it re-raises), so the runner can still book it.
    """

    paid_usd: float = 0.0
    paid_ticks: int = 0


class LiveCallsDisabled(XaiError):
    """A live xAI call was attempted before M7."""


class ExecPath(StrEnum):
    SERVER_SIDE = "server_side_mcp"  # xAI calls the remote MCP server itself
    CLIENT_SIDE = "client_side_functions"  # we execute function calls the model emits


@dataclass(frozen=True)
class ModelConfig:
    """One row of the model matrix. ``model_id``/``effort`` may be UNRESOLVED."""

    label: str
    model_id: str
    effort: str | None  # None = the model takes no effort setting
    role: str  # flagship (the only row in this budget)

    @property
    def resolved(self) -> bool:
        return UNRESOLVED not in (self.model_id, self.effort)


# Owner decision (issue #25): grok-4.7 only. Model id, 500k context, knowledge cutoff May 2026:
# https://docs.x.ai/developers/models/grok-4.7 and https://docs.x.ai/developers/grok-4-7
# (retrieved 2026-10-11). Efforts the API accepts: low | medium | high (default) | xhigh
# (https://docs.x.ai/developers/model-capabilities/text/reasoning). The effort used here
# (medium) is OWNER-APPROVED (2026-10-11): see docs/PREREGISTRATION.md. The multi-agent model
# (grok-4.20-multi-agent-0309), the small/fast row and a second effort row are out of scope for
# this budget (future work).
EFFORT_LEVELS = ("low", "medium", "high", "xhigh")
MODEL_CONFIGS: tuple[ModelConfig, ...] = (
    ModelConfig("grok-4.7", "grok-4.7", "medium", "flagship"),
)


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens, with the page it came from.

    A request whose prompt is ``long_context_tokens`` or more bills every token at the
    ``long_*`` rates. ``cached_input_per_mtok`` is recorded for the docs; the planning cost
    ignores caching, so it is an upper bound. The authoritative cost is the provider's
    ``cost_in_usd_ticks`` when the response carries it.
    """

    input_per_mtok: float
    output_per_mtok: float
    source: str
    cached_input_per_mtok: float | None = None
    long_context_tokens: int | None = None
    long_input_per_mtok: float | None = None
    long_output_per_mtok: float | None = None

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        """Estimated cost of one request."""
        long_ = self.long_context_tokens is not None and input_tokens >= self.long_context_tokens
        if long_ and self.long_input_per_mtok is not None and self.long_output_per_mtok is not None:
            rin, rout = self.long_input_per_mtok, self.long_output_per_mtok
        else:
            rin, rout = self.input_per_mtok, self.output_per_mtok
        return (input_tokens * rin + output_tokens * rout) / 1e6


GROK_4_7_PRICING = Pricing(
    2.00,
    6.00,
    "https://docs.x.ai/developers/models/grok-4.7 and https://docs.x.ai/docs/models "
    "(retrieved 2026-10-11)",
    cached_input_per_mtok=0.50,
    long_context_tokens=200_000,
    long_input_per_mtok=4.00,
    long_output_per_mtok=12.00,
)

# usage.cost_in_usd_ticks / 1e10 = USD (https://docs.x.ai/developers/cost-tracking)
TICKS_PER_USD = 1e10

# model label -> Pricing; None = UNRESOLVED. No price is invented.
PRICING: Mapping[str, Pricing | None] = {"grok-4.7": GROK_4_7_PRICING}


@dataclass(frozen=True)
class ToolCallRequest:
    call_id: str
    name: str
    args: Mapping[str, Any]


@dataclass(frozen=True)
class RemoteMcp:
    """A remote MCP server spec for the server-side path.

    ``token_env`` names the env var holding the bearer token; the value is never stored.
    ``local`` is a test-only in-process stand-in for the network hop.
    """

    url: str
    token_env: str
    local: ToolDispatcher | None = None


@dataclass(frozen=True)
class ChatRequest:
    model: str
    effort: str | None
    messages: tuple[Mapping[str, Any], ...]
    tools: tuple[Mapping[str, Any], ...] = ()  # function specs, client-side path
    remote_mcp: RemoteMcp | None = None  # server-side path


@dataclass(frozen=True)
class ChatResponse:
    served_model: str
    content: str
    input_tokens: int
    output_tokens: int
    tool_calls: tuple[ToolCallRequest, ...] = ()  # to execute (client-side path)
    remote_calls: tuple[ToolCallRequest, ...] = ()  # xAI executed these (server-side path)
    cost_in_usd_ticks: int | None = None  # usage.cost_in_usd_ticks, when the response has it
    # the provider's output items, verbatim, echoed back next turn (encrypted reasoning must be
    # passed back unchanged); empty for scripted responses
    output_items: tuple[Mapping[str, Any], ...] = ()


class XaiClient(Protocol):
    def chat(self, request: ChatRequest) -> ChatResponse: ...


class ScriptedClient:
    """Replays prepared responses in order; the mock for every test. Records its requests.

    On the server-side path, ``remote_calls`` in a scripted response are run through
    ``request.remote_mcp.local``, standing in for xAI calling the MCP server.
    """

    def __init__(self, script: Sequence[ChatResponse | Exception]) -> None:
        self._script = list(script)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if not self._script:
            raise XaiError("scripted client has no response left")
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        if request.remote_mcp is not None and step.remote_calls:
            local = request.remote_mcp.local
            if local is None:
                raise XaiError("remote MCP has no in-process stand-in")
            for call in step.remote_calls:
                local(call.name, call.args)
        return step


# --- live client (M7) -------------------------------------------------------------------------
# CONFIRMED by the owner from docs.x.ai (retrieved 2026-10-11; issue #25 and the PR #30 review):
# * https://docs.x.ai/docs/guides/function-calling: tools are flat
#   {"type": "function", "name", "description", "parameters"}; output items are ``function_call``
#   (``call_id``, ``name``, ``arguments`` as a JSON string); results go back as
#   {"type": "function_call_output", "call_id", "output"}; text is in ``message`` items
#   (``content[0].text``); ``parallel_tool_calls`` is on by default.
# * https://docs.x.ai/developers/cost-tracking: ``usage.input_tokens``, ``usage.output_tokens`` and
#   ``usage.cost_in_usd_ticks`` are on Responses API bodies; the ticks figure is per request (not
#   cumulative) and net of caching discounts; 1 USD = 1e10 ticks.
# * POST https://api.x.ai/v1/responses; ``reasoning.effort``; encrypted reasoning items are passed
#   back unchanged each turn.
# STILL UNCONFIRMED: the HTTP status for quota. 429 is treated as a halt (resumable). The parser
# stays strict: unknown output item or message part types raise. The cached-token usage field is
# ignored (the ticks figure already includes discounts).
WIRE_CONFIRMED = True
API_URL = "https://api.x.ai/v1/responses"

# (url, headers, body, timeout) -> (status, body bytes). Tests pass a recorded fake.
Transport = Callable[[str, Mapping[str, str], bytes, float], tuple[int, bytes]]


class UsageUnrecognised(QuotaExhausted):
    """A response lacked a usage figure we need. Halts the runner: cost is never guessed."""


def urllib_transport(
    url: str, headers: Mapping[str, str], body: bytes, timeout: float
) -> tuple[int, bytes]:  # pragma: no cover - the only network code; tests inject a fake
    req = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise XaiError(f"transport failure: {type(exc).__name__}") from exc


def _to_input(messages: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    items: list[Mapping[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            items.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            if not m.get("output_items"):
                raise XaiError("assistant turn has no provider output items to echo back")
            items.extend(m["output_items"])
        elif role == "tool":
            call_id, out = m["tool_call_id"], m["content"]
            items.append({"type": "function_call_output", "call_id": call_id, "output": out})
        else:
            raise XaiError(f"unsupported message role {role!r}")
    return items


def _count(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UsageUnrecognised(f"usage.{what} missing or not a non-negative integer")
    return value


def parse_response(body: Mapping[str, Any]) -> ChatResponse:
    """Strict parse of one Responses API body; anything unexpected raises (never guessed)."""
    if body.get("error"):
        raise XaiError("API returned an error object")
    if body.get("status", "completed") != "completed":
        raise XaiError(f"response status {body.get('status')!r}")
    usage = body.get("usage")
    if not isinstance(usage, dict):
        raise UsageUnrecognised("response has no usage object")
    n_in = _count(usage.get("input_tokens"), "input_tokens")
    n_out = _count(usage.get("output_tokens"), "output_tokens")
    ticks = _count(usage.get("cost_in_usd_ticks"), "cost_in_usd_ticks")
    served, output = body.get("model"), body.get("output")
    if not isinstance(served, str) or not served or not isinstance(output, list):
        raise XaiError("response lacks a model id or output list")
    text: list[str] = []
    calls: list[ToolCallRequest] = []
    for item in output:
        kind = item.get("type") if isinstance(item, dict) else None
        if kind == "message":
            for part in item.get("content", []):
                if part.get("type") != "output_text":
                    raise XaiError(f"unknown message part {part.get('type')!r}")
                text.append(str(part["text"]))
        elif kind == "function_call":
            try:
                args = json.loads(item["arguments"])
                call = ToolCallRequest(item["call_id"], item["name"], args)
            except (KeyError, TypeError, ValueError) as exc:
                raise XaiError("malformed function_call item") from exc
            if not isinstance(args, dict):
                raise XaiError("function_call arguments are not an object")
            calls.append(call)
        elif kind != "reasoning":
            raise XaiError(f"unknown output item type {kind!r}")
    return ChatResponse(served, "".join(text), n_in, n_out, tuple(calls), (), ticks, tuple(output))


class LiveClient:
    """Responses API client for the client-side function-calling path. Refuses while the wire
    format is unconfirmed, on the server-side path (deferred), and on any unexpected reply.

    HTTP 429 raises :class:`QuotaExhausted` (the runner halts, resumable): the docs read here do
    not say whether 429 means quota or rate limit, and halting is the safe reading.
    """

    def __init__(
        self,
        api_key: str,
        transport: Transport = urllib_transport,
        timeout_s: float = 600.0,
        wire_confirmed: bool | None = None,
    ) -> None:
        if not api_key:
            raise LiveCallsDisabled("no API key supplied")
        self._key = api_key
        self._transport, self._timeout = transport, timeout_s
        self._confirmed = WIRE_CONFIRMED if wire_confirmed is None else wire_confirmed

    def __repr__(self) -> str:  # never show the key
        return "LiveClient(<redacted>)"

    def chat(self, request: ChatRequest) -> ChatResponse:
        if not self._confirmed:
            raise LiveCallsDisabled("Responses API wire format is UNRESOLVED (xai.WIRE_CONFIRMED)")
        if request.remote_mcp is not None:
            raise LiveCallsDisabled("server-side remote MCP is deferred")
        body: dict[str, Any] = {
            "model": request.model,
            "input": _to_input(request.messages),
            "tools": [{"type": "function", **t["function"]} for t in request.tools],
        }
        if request.effort is not None:
            body["reasoning"] = {"effort": request.effort}
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        status, raw = self._transport(API_URL, headers, json.dumps(body).encode(), self._timeout)
        if status == 429:
            raise QuotaExhausted("HTTP 429")
        if status != 200:
            raise XaiError(f"HTTP {status}")  # the body is not echoed: it could carry anything
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise XaiError("response is not JSON") from exc
        if not isinstance(parsed, dict):
            raise XaiError("response is not a JSON object")
        return parse_response(parsed)
