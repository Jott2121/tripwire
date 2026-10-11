"""One benchmark run (M5): a Grok agent, a task, a fault cell, one execution path.

``run_one`` builds a fresh store, ledger and fault injector for the run, serves the five tools
in-process, drives the agent through :class:`~tripwire.xai.XaiClient` on either path, and scores
the final verdict with the existing adjudicator. Nothing here reads the environment or the clock
directly: the clock and timer are arguments, so a run is reproducible from its plan and script.

Exclusion codes are sabot's (``RUN_ERROR > BASELINE_FAIL > INJECTION_UNVERIFIED``). A run is never
dropped from the receipt: an excluded run keeps its record and carries the code. BASELINE_FAIL
needs the paired clean run, so the analysis (M7) applies it; the harness applies the other two.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from mcp.shared.memory import create_connected_server_and_client_session

from tripwire import synthdata
from tripwire.adjudicator import Adjudication, Verdict, VerdictError, adjudicate
from tripwire.faults import Clock, FaultConfig, FaultInjector, FaultSpec
from tripwire.gate import Gate
from tripwire.ledger import Ledger, LedgerError
from tripwire.prompts import Condition, condition_prompt
from tripwire.server import build_mcp
from tripwire.store import Store
from tripwire.tasks import TASK_DATA_SEED, PlannedRun, Task
from tripwire.xai import (
    TICKS_PER_USD,
    ChatRequest,
    ChatResponse,
    ExecPath,
    ModelConfig,
    Pricing,
    QuotaExhausted,
    RemoteMcp,
    ToolCallRequest,
    XaiClient,
    XaiError,
)

MAX_TURNS = 12  # OWNER-APPROVED (2026-10-11): client-side loop cap; hitting it scores incorrect


class Exclusion(StrEnum):
    RUN_ERROR = "RUN_ERROR"  # the client failed (transport, API error)
    BASELINE_FAIL = "BASELINE_FAIL"  # applied at analysis: the paired clean run was incorrect
    INJECTION_UNVERIFIED = "INJECTION_UNVERIFIED"  # planned fault never fired, or ledger bad


class GateUnavailable(RuntimeError):
    """The gate cannot run on this path (server-side results never reach the harness): refuse."""


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    task_id: str
    planned_fault: str | None
    condition: str
    path: str
    model_label: str
    model_requested: str
    effort: str | None
    served_models: tuple[str, ...]  # one per API call, in order: provenance, never assumed
    input_tokens: int
    output_tokens: int
    cost_usd: float | None  # per request: provider ticks when present, else the price estimate
    cost_in_usd_ticks: int | None  # sum of usage.cost_in_usd_ticks over requests that had it
    latency_s: float
    timestamp: str
    turns: int
    tool_calls: tuple[str, ...]
    verdict: dict[str, Any] | None  # None = no parseable verdict (scored incorrect)
    adjudication: dict[str, Any] | None
    exclusion: str | None
    error: str | None = None
    turn_cap_hit: bool = False  # the loop ended at MAX_TURNS with no reply: scored incorrect
    gate: dict[str, Any] | None = None  # gate condition only: checks, failures, block, cost

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


class InProcessTools:
    """The five tools served from one run's faulted server, called over the SDK's memory
    transport. Tool errors come back as ``{"error": text}``, as the model would see them."""

    def __init__(self, store: Store, injector: FaultInjector, clock: Clock) -> None:
        self._mcp = build_mcp(store, clock=clock, faults=injector)

    def specs(self) -> tuple[dict[str, Any], ...]:
        tools = asyncio.run(self._list())
        return tuple(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.inputSchema,
                },
            }
            for t in tools
        )

    async def _list(self) -> list[Any]:
        async with create_connected_server_and_client_session(self._mcp) as session:
            return list((await session.list_tools()).tools)

    def call(self, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        async def go() -> dict[str, Any]:
            async with create_connected_server_and_client_session(self._mcp) as session:
                res = await session.call_tool(name, dict(args))
            if res.isError:
                return {"error": "".join(getattr(c, "text", "") for c in res.content)}
            return dict(res.structuredContent or {})

        return asyncio.run(go())


def extract_verdict(text: str) -> Verdict | None:
    """The last JSON object in the reply that parses as a verdict; ``None`` if there is none."""
    decoder, found = json.JSONDecoder(), None
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text, i)
            found = Verdict.from_dict(obj)
        except (ValueError, VerdictError):
            continue
    return found


def _no_verdict(run_id: str, injected: tuple[str, ...]) -> Adjudication:
    """Scored incorrect, not flagged, not acted: a malformed reply is never excluded."""
    return Adjudication(run_id, injected, False, False, False, (), False, 0, len(injected), None)


def run_one(
    plan: PlannedRun,
    task: Task,
    model: ModelConfig,
    path: ExecPath,
    condition: Condition,
    client: XaiClient,
    *,
    run_id: str,
    clock: Clock,
    pricing: Pricing | None = None,
    mcp_url: str = "UNRESOLVED",
    token_env: str = "TRIPWIRE_SERVER_TOKEN",
    ledger_path: Path | None = None,
    timer: Callable[[], float] = time.perf_counter,
) -> RunRecord:
    """Run and score one agent run. ``QuotaExhausted`` propagates so the runner can halt."""
    if condition is Condition.GATE and path is ExecPath.SERVER_SIDE:
        raise GateUnavailable("server-side tool results run at xAI and never reach the gate")
    store = Store.from_dataset(synthdata.generate(TASK_DATA_SEED))
    ledger = Ledger(ledger_path)
    specs = (FaultSpec(plan.fault),) if plan.fault else ()
    injector = FaultInjector(FaultConfig(plan.fault_seed, specs), ledger, run_id, store, clock)
    tools = InProcessTools(store, injector, clock)
    # the gate reads only the store's spec registry (faults alter what is served, not the store)
    gate = Gate(store) if condition is Condition.GATE else None
    gate_cost: dict[str, Any] = {"extra_tool_calls": 0, "latency_s": 0.0}
    messages: list[Mapping[str, Any]] = [
        {"role": "user", "content": condition_prompt(task, condition)}
    ]
    served: list[str] = []
    names: list[str] = []
    tokens = [0, 0]
    ticks: list[int] = []
    costs: list[float | None] = []
    latency = 0.0
    turns = 0
    final: str | None = None
    error: str | None = None

    def ask(req: ChatRequest) -> ChatResponse:
        nonlocal latency, turns
        start = timer()
        resp = client.chat(req)
        latency += timer() - start
        turns += 1
        served.append(resp.served_model)
        tokens[0] += resp.input_tokens
        tokens[1] += resp.output_tokens
        if resp.cost_in_usd_ticks is not None:  # the provider's figure beats our estimate
            ticks.append(resp.cost_in_usd_ticks)
            costs.append(resp.cost_in_usd_ticks / TICKS_PER_USD)
        else:
            costs.append(pricing.cost(resp.input_tokens, resp.output_tokens) if pricing else None)
        return resp

    try:
        if path is ExecPath.SERVER_SIDE:
            remote = RemoteMcp(mcp_url, token_env, local=tools.call)
            resp = ask(
                ChatRequest(model.model_id, model.effort, tuple(messages), remote_mcp=remote)
            )
            names += [c.name for c in resp.remote_calls]
            final = resp.content
        else:
            specs_ = tools.specs()
            while turns < MAX_TURNS and final is None:
                resp = ask(ChatRequest(model.model_id, model.effort, tuple(messages), specs_))
                if not resp.tool_calls:
                    final = resp.content
                    break
                messages.append(
                    {
                        "role": "assistant",
                        "content": resp.content,
                        "tool_calls": [_wire(c) for c in resp.tool_calls],
                    }
                )
                for call in resp.tool_calls:
                    names.append(call.name)
                    out = tools.call(call.name, call.args)
                    if gate is not None:
                        began = timer()
                        _gate_tool(gate, tools, call, out, gate_cost)
                        gate_cost["latency_s"] += timer() - began
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.call_id,
                            "content": json.dumps(out, sort_keys=True),
                        }
                    )
    except QuotaExhausted:
        raise
    except XaiError as exc:
        error = f"{type(exc).__name__}: {exc}"

    agent_verdict = extract_verdict(final) if final is not None else None
    if gate is not None and final is not None:
        began = timer()
        final = gate.report(final)
        gate_cost["latency_s"] += timer() - began
    verdict = extract_verdict(final) if final is not None else None
    adj: Adjudication | None = None
    exclusion: Exclusion | None = Exclusion.RUN_ERROR if error else None
    if not error:
        try:
            adj = (
                adjudicate(ledger.events, ledger.head, store, task, verdict, run_id)
                if verdict
                else _no_verdict(run_id, tuple(e.fault for e in ledger.events))
            )
            # re-reading refires the fault (rate 1.0): only a missing or foreign kind is unverified
            if plan.fault and set(adj.injected) != {plan.fault.value}:
                exclusion = Exclusion.INJECTION_UNVERIFIED
        except LedgerError:
            exclusion = Exclusion.INJECTION_UNVERIFIED
    return RunRecord(
        run_id=run_id,
        task_id=task.task_id,
        planned_fault=plan.fault.value if plan.fault else None,
        condition=condition.value,
        path=path.value,
        model_label=model.label,
        model_requested=model.model_id,
        effort=model.effort,
        served_models=tuple(served),
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        cost_usd=None if None in costs else sum(c for c in costs if c is not None),
        cost_in_usd_ticks=sum(ticks) if ticks else None,
        latency_s=latency,
        timestamp=_iso(clock()),
        turns=turns,
        tool_calls=tuple(names),
        verdict=_verdict_dict(verdict),
        adjudication=asdict(adj) if adj else None,
        exclusion=exclusion.value if exclusion else None,
        error=error,
        turn_cap_hit=final is None and error is None and turns >= MAX_TURNS,
        gate=_gate_dict(gate, gate_cost, agent_verdict) if gate is not None else None,
    )


def _gate_tool(
    gate: Gate,
    tools: InProcessTools,
    call: ToolCallRequest,
    out: dict[str, Any],
    cost: dict[str, Any],
) -> None:
    """Check one result; read back every write through the same (faultable) server."""
    gate.observe(call.name, call.args, out)
    if call.name == "create_work_order" and "error" not in out:
        wo = out.get("work_order")
        wo_id = wo.get("work_order_id", "") if isinstance(wo, dict) else ""
        back = tools.call("get_work_order", {"work_order_id": wo_id})
        cost["extra_tool_calls"] += 1
        gate.verify_write(call.args, out, back)


def _gate_dict(gate: Gate, cost: dict[str, Any], agent: Verdict | None) -> dict[str, Any]:
    """The gate's receipt. On the client-side path the gate makes no model call, so its added
    tokens are 0 by construction; its cost is the read-back calls and its own latency."""
    return {
        **gate.summary(),
        **cost,
        "added_tokens": 0,
        "agent_verdict": _verdict_dict(agent) if gate.blocked else None,
    }


def _wire(call: ToolCallRequest) -> dict[str, Any]:
    return {
        "id": call.call_id,
        "type": "function",
        "function": {"name": call.name, "arguments": json.dumps(dict(call.args))},
    }


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _verdict_dict(verdict: Verdict | None) -> dict[str, Any] | None:
    if verdict is None:
        return None
    return {
        "status": verdict.status.value,
        "value": verdict.value,
        "unit": verdict.unit.value if verdict.unit else None,
        "work_order_id": verdict.work_order_id,
        "flags": list(verdict.flags),
    }
