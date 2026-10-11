"""xAI client interface, model configs, and pricing (M5). No network, no environment reads.

The harness talks to xAI only through :class:`XaiClient`. Tests use :class:`ScriptedClient`;
:class:`LiveClient` refuses every call until M7 wires a real one, so a live call cannot happen
by accident. ``XAI_API_KEY`` is never read here.

Model ids, reasoning-effort levels, and prices could not be read from docs.x.ai when this was
written (the fetch was not permitted), so each is :data:`UNRESOLVED` rather than a guess. A run
against an UNRESOLVED model or price is refused by the runner. Fill them from the docs, with the
URL and the retrieval date, before M7.
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
    role: str  # flagship-high | flagship-low | small-fast | multi-agent

    @property
    def resolved(self) -> bool:
        return UNRESOLVED not in (self.model_id, self.effort)


# Targets from the M5 brief; every id and level is UNRESOLVED until read from xAI's docs.
MODEL_CONFIGS: tuple[ModelConfig, ...] = (
    ModelConfig("flagship-high", UNRESOLVED, UNRESOLVED, "flagship-high"),
    ModelConfig("flagship-low", UNRESOLVED, UNRESOLVED, "flagship-low"),
    ModelConfig("small-fast", UNRESOLVED, None, "small-fast"),
    ModelConfig("multi-agent", UNRESOLVED, None, "multi-agent"),
)


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens, with the page it came from."""

    input_per_mtok: float
    output_per_mtok: float
    source: str

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok) / 1e6


# model label -> Pricing; None = UNRESOLVED. No price is invented.
PRICING: Mapping[str, Pricing | None] = {m.label: None for m in MODEL_CONFIGS}


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
