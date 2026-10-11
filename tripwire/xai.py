"""xAI client interface, model configs, and pricing (M5). No network, no environment reads.

The harness talks to xAI only through :class:`XaiClient`. Tests use :class:`ScriptedClient`;
:class:`LiveClient` refuses every call until M7 wires a real one, so a live call cannot happen
by accident. ``XAI_API_KEY`` is never read here.

The model id, effort levels and prices below were supplied by the owner in issue #25, retrieved
from docs.x.ai on 2026-10-11 (sources next to each value). Anything not in that issue stays
:data:`UNRESOLVED`, and a run against an UNRESOLVED model or price is refused by the runner.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

UNRESOLVED = "UNRESOLVED"

ToolDispatcher = Callable[[str, Mapping[str, Any]], dict[str, Any]]


class XaiError(RuntimeError):
    """The client could not complete a request (a RUN_ERROR, never a score)."""


class QuotaExhausted(XaiError):
    """The account's quota or rate budget is spent: the runner halts instead of retrying."""


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


class LiveClient:
    """Placeholder for the real client (M7). Every call is refused."""

    def chat(self, request: ChatRequest) -> ChatResponse:
        raise LiveCallsDisabled("live xAI calls are not enabled before M7")
