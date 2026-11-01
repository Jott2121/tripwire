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

from tripwire import synthdata, tasks_v2
from tripwire.adjudicator import (
    FLAG_VOCABULARY,
    FLAG_VOCABULARY_V2,
    Adjudication,
    Verdict,
    VerdictError,
    adjudicate,
)
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


SHADOW_VARIANTS = ("contract", "crosscheck")  # v2: gate-contract and gate-crosscheck
TASK_SETS = ("v1", "v2")  # which dataset the run's store is built from and which flag words


def _task_store(task_set: str, task: Task) -> Store:
    """A fresh clean store for ``task``'s task set; a task from another set is refused."""
    if task_set not in TASK_SETS:
        raise ValueError(f"task_set must be one of {TASK_SETS}, got {task_set!r}")
    if task.task_id.startswith(tasks_v2.TASK_PREFIX) != (task_set == "v2"):
        raise ValueError(f"task {task.task_id} is not a {task_set} task")
    if task_set == "v2":
        return tasks_v2.default_store()
    return Store.from_dataset(synthdata.generate(TASK_DATA_SEED))


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
    cached_input_tokens: int = 0  # parts of input_tokens read from / written to a prompt cache
    cache_write_tokens: int = 0
    refused: bool = False  # the provider ended the final turn as a refusal
    # v2 shadow gates: variant -> {"verdict", "adjudication", "gate"}, scored on this same
    # trajectory (the agent never sees a shadow gate). None when no shadow gate ran.
    shadow: dict[str, Any] | None = None
    task_set: str = "v1"

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
    shadow_gates: tuple[str, ...] = (),
    task_set: str = "v1",
) -> RunRecord:
    """Run and score one agent run. ``QuotaExhausted`` propagates so the runner can halt.

    ``shadow_gates`` (v2) scores each named gate variant on this run's own trajectory: every
    tool result the agent sees is also checked by the shadow gate, every write is read back once
    through the faulted server on an isolated random stream, and the gate's final decision is
    adjudicated separately. Nothing a shadow gate does reaches the agent, so the raw adjudication
    is exactly the ungated run's.

    ``task_set`` picks the dataset the run's store is built from and the flag words the prompt
    lists: ``"v1"`` (the default, unchanged) or ``"v2"`` (``tasks_v2``, ``FLAG_VOCABULARY_V2``).
    """
    if condition is Condition.GATE and path is ExecPath.SERVER_SIDE:
        raise GateUnavailable("server-side tool results run at xAI and never reach the gate")
    if shadow_gates and (condition is Condition.GATE or path is ExecPath.SERVER_SIDE):
        raise GateUnavailable("shadow gates score ungated client-side runs only")
    unknown = set(shadow_gates) - set(SHADOW_VARIANTS)
    if unknown or len(set(shadow_gates)) != len(shadow_gates):
        raise ValueError(f"shadow gates must be distinct names from {SHADOW_VARIANTS}")
    store = _task_store(task_set, task)
    vocabulary = FLAG_VOCABULARY_V2 if task_set == "v2" else FLAG_VOCABULARY
    ledger = Ledger(ledger_path)
    specs = (FaultSpec(plan.fault),) if plan.fault else ()
    injector = FaultInjector(FaultConfig(plan.fault_seed, specs), ledger, run_id, store, clock)
    tools = InProcessTools(store, injector, clock)
    # the gate reads only the store's spec registry (faults alter what is served, not the store)
    gate = Gate(store) if condition is Condition.GATE else None
    gate_cost: dict[str, Any] = {"extra_tool_calls": 0, "latency_s": 0.0, "read_back_faults": []}
    shadows = {v: Gate(store, crosscheck=v == "crosscheck") for v in shadow_gates}
    shadow_cost: dict[str, Any] = {"extra_tool_calls": 0, "latency_s": 0.0, "read_back_faults": []}
    messages: list[Mapping[str, Any]] = [
        {"role": "user", "content": condition_prompt(task, condition, vocabulary)}
    ]
    served: list[str] = []
    names: list[str] = []
    tokens = [0, 0, 0, 0]  # input, output, cached read, cache write
    refused = False
    ticks: list[int] = []
    costs: list[float | None] = []
    latency = 0.0
    turns = 0
    final: str | None = None
    error: str | None = None

    def ask(req: ChatRequest) -> ChatResponse:
        nonlocal latency, turns, refused
        start = timer()
        resp = client.chat(req)
        latency += timer() - start
        turns += 1
        served.append(resp.served_model)
        tokens[0] += resp.input_tokens
        tokens[1] += resp.output_tokens
        tokens[2] += resp.cached_input_tokens
        tokens[3] += resp.cache_write_tokens
        refused = refused or resp.refused
        if resp.cost_in_usd_ticks is not None:  # the provider's figure beats our estimate
            ticks.append(resp.cost_in_usd_ticks)
            costs.append(resp.cost_in_usd_ticks / TICKS_PER_USD)
        else:
            costs.append(
                pricing.cost(
                    resp.input_tokens,
                    resp.output_tokens,
                    resp.cached_input_tokens,
                    resp.cache_write_tokens,
                )
                if pricing
                else None
            )
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
                        **({"output_items": list(resp.output_items)} if resp.output_items else {}),
                    }
                )
                for call in resp.tool_calls:
                    names.append(call.name)
                    out = tools.call(call.name, call.args)
                    if gate is not None:
                        began = timer()
                        _gate_tool(gate, tools, injector, call, out, gate_cost)
                        gate_cost["latency_s"] += timer() - began
                    if shadows:
                        began = timer()
                        _shadow_tool(shadows, tools, injector, call, out, shadow_cost)
                        shadow_cost["latency_s"] += timer() - began
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.call_id,
                            "content": json.dumps(out, sort_keys=True),
                        }
                    )
    except QuotaExhausted as exc:
        exc.paid_usd = sum(c for c in costs if c is not None)  # lets the runner book what ran
        exc.paid_ticks = sum(ticks)
        raise
    except XaiError as exc:
        error = f"{type(exc).__name__}: {exc}"

    agent_verdict = extract_verdict(final) if final is not None else None
    turn_cap_hit = final is None and error is None and turns >= MAX_TURNS
    began = timer()
    verdict = _gated_verdict(gate, final, turn_cap_hit) if gate else agent_verdict
    gate_cost["latency_s"] += timer() - began if gate else 0.0

    def score(v: Verdict | None) -> tuple[Adjudication | None, Exclusion | None]:
        if error:
            return None, Exclusion.RUN_ERROR
        try:
            a = (
                adjudicate(ledger.events, ledger.head, store, task, v, run_id)
                if v
                else _no_verdict(run_id, tuple(e.fault for e in ledger.events))
            )
        except LedgerError:
            return None, Exclusion.INJECTION_UNVERIFIED
        # re-reading refires the fault (rate 1.0): only a missing or foreign kind is unverified
        if plan.fault and set(a.injected) != {plan.fault.value}:
            return a, Exclusion.INJECTION_UNVERIFIED
        return a, None

    adj, exclusion = score(verdict)
    shadow_out: dict[str, Any] | None = None
    if shadows:
        shadow_out = {}
        for name, sg in shadows.items():
            sv = _gated_verdict(sg, final, turn_cap_hit)
            s_adj, _ = score(sv)  # the run's exclusion is the raw run's; it is shared
            shadow_out[name] = {
                "verdict": _verdict_dict(sv),
                "adjudication": asdict(s_adj) if s_adj else None,
                "gate": {**sg.summary(), **shadow_cost, "added_tokens": 0},
            }
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
        turn_cap_hit=turn_cap_hit,
        gate=_gate_dict(gate, gate_cost, agent_verdict) if gate is not None else None,
        cached_input_tokens=tokens[2],
        cache_write_tokens=tokens[3],
        refused=refused,
        shadow=shadow_out,
        task_set=task_set,
    )


def _gated_verdict(gate: Gate, final: str | None, turn_cap_hit: bool) -> Verdict | None:
    """The verdict once ``gate`` has decided the final report (live gate and shadow alike)."""
    if turn_cap_hit and gate.blocked:
        # amendment A3: the gate already blocked a fault, so its fail-closed report is the verdict
        final = ""
    if final is None:
        return None
    verdict = extract_verdict(gate.report(final))
    if verdict is not None:
        # the gate verified the served data against the store: drop an agent refusal it disproves
        verdict = gate.clear_unsupported(verdict)
    return verdict


def _shadow_tool(
    gates: Mapping[str, Gate],
    tools: InProcessTools,
    injector: FaultInjector,
    call: ToolCallRequest,
    out: dict[str, Any],
    cost: dict[str, Any],
) -> None:
    """Like :func:`_gate_tool` for every shadow gate, with ONE read-back per write on the
    injector's isolated stream, so the agent's own fault stream is never advanced."""
    for g in gates.values():
        g.observe(call.name, call.args, out)
    if call.name == "create_work_order" and "error" not in out:
        wo = out.get("work_order")
        wo_id = wo.get("work_order_id", "") if isinstance(wo, dict) else ""
        with injector.side_ledger(isolated=True) as side:
            back = tools.call("get_work_order", {"work_order_id": wo_id})
        cost["extra_tool_calls"] += 1
        cost["read_back_faults"] += side
        for g in gates.values():
            g.verify_write(call.args, out, back)


def _gate_tool(
    gate: Gate,
    tools: InProcessTools,
    injector: FaultInjector,
    call: ToolCallRequest,
    out: dict[str, Any],
    cost: dict[str, Any],
) -> None:
    """Check one result; read back every write through the same (faultable) server."""
    gate.observe(call.name, call.args, out)
    if call.name == "create_work_order" and "error" not in out:
        wo = out.get("work_order")
        wo_id = wo.get("work_order_id", "") if isinstance(wo, dict) else ""
        # a fault the read-back fires was never shown to the agent: keep it out of the run
        # ledger (it would score as injected) and report it on the receipt instead
        with injector.side_ledger() as side:
            back = tools.call("get_work_order", {"work_order_id": wo_id})
        cost["extra_tool_calls"] += 1
        cost["read_back_faults"] += side
        gate.verify_write(call.args, out, back)


def _gate_dict(gate: Gate, cost: dict[str, Any], agent: Verdict | None) -> dict[str, Any]:
    """The gate's receipt. On the client-side path the gate makes no model call, so its added
    tokens are 0 by construction; its cost is the read-back calls and its own latency."""
    return {
        **gate.summary(),
        **cost,
        "added_tokens": 0,
        # keep the pre-gate verdict whenever the gate overrode the agent (a block or a downgrade)
        "agent_verdict": _verdict_dict(agent) if (gate.blocked or gate.downgraded) else None,
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
