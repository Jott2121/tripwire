"""v2 shadow gates: scored on the agent's own trajectory, never visible to the agent."""

from __future__ import annotations

import json

import pytest
import support as sp

from tripwire import tasks as tk
from tripwire.faults import FaultConfig, FaultInjector, FaultKind, FaultSpec
from tripwire.harness import SHADOW_VARIANTS, GateUnavailable, run_one
from tripwire.ledger import Ledger
from tripwire.prompts import Condition
from tripwire.xai import ExecPath, ScriptedClient, ToolCallRequest, XaiError

WO_TASK = sp.first_task(tk.TaskKind.WORK_ORDER, FaultKind.F5)


def _wo_script(task: tk.Task, final: str) -> list:
    """Write the order, read it back as the agent, read it again, then answer."""
    write = next(c for c in task.calls if c.tool == "create_work_order")
    wo_id = json.loads(sp.truth_json(task))["work_order_id"]
    get = ToolCallRequest("g1", "get_work_order", {"work_order_id": wo_id})
    again = ToolCallRequest("g2", "get_work_order", {"work_order_id": wo_id})
    return [
        sp.resp(calls=[ToolCallRequest("w1", write.tool, write.args)]),
        sp.resp(calls=[get]),
        sp.resp(calls=[again]),
        sp.resp(final),
    ]


def _run(task, fault, script, condition=Condition.PROMPT_FIX, shadow=(), seed=7):
    client = ScriptedClient(script)
    rec = run_one(
        sp.plan(task, fault, seed), task, sp.MODEL, ExecPath.CLIENT_SIDE, condition, client,
        run_id="run-1", clock=sp.CLOCK, pricing=sp.PRICE, shadow_gates=shadow,
    )  # fmt: skip
    return rec, client


def _sent(client: ScriptedClient) -> str:
    return json.dumps([list(r.messages) for r in client.requests], sort_keys=True, default=str)


def test_shadow_gates_leave_every_agent_visible_message_byte_identical():
    final = sp.truth_json(WO_TASK)
    plain, c_plain = _run(WO_TASK, FaultKind.F5, _wo_script(WO_TASK, final))
    shadowed, c_shadow = _run(WO_TASK, FaultKind.F5, _wo_script(WO_TASK, final),
                              shadow=SHADOW_VARIANTS)  # fmt: skip
    assert _sent(c_plain) == _sent(c_shadow)
    assert plain.adjudication == shadowed.adjudication and plain.verdict == shadowed.verdict
    assert plain.shadow is None and set(shadowed.shadow) == set(SHADOW_VARIANTS)
    for out in shadowed.shadow.values():
        assert out["gate"]["extra_tool_calls"] == 1  # one read-back per write, shared


def test_the_live_gate_shares_the_stream_which_is_why_shadows_isolate_it():
    """Without isolation the read-back advances the fault stream: the agent's own next read is
    cut differently under the live v1 gate than with no gate at all."""
    final = sp.truth_json(WO_TASK)
    _, c_plain = _run(WO_TASK, FaultKind.F5, _wo_script(WO_TASK, final))
    _, c_live = _run(WO_TASK, FaultKind.F5, _wo_script(WO_TASK, final), Condition.GATE)

    def tool_results(client):
        return [m["content"] for m in client.requests[-1].messages if m["role"] == "tool"]

    assert tool_results(c_plain)[0] == tool_results(c_live)[0]  # the write result is the same
    assert tool_results(c_plain)[1:] != tool_results(c_live)[1:]  # later reads diverge


@pytest.mark.parametrize(
    ("kind", "fault"),
    [
        (tk.TaskKind.SPEC_LIMIT, FaultKind.F2),
        (tk.TaskKind.SPEC_LIMIT, FaultKind.F1),
        (tk.TaskKind.TEST_MEASURED, FaultKind.F3),
        (tk.TaskKind.SPEC_LIMIT, FaultKind.F6),
        (tk.TaskKind.DEFECT_COUNT, FaultKind.F5),
    ],
)
def test_shadow_crosscheck_decides_exactly_as_the_live_gate_on_read_faults(kind, fault):
    task = sp.first_task(kind, fault)
    final = sp.truth_json(task)
    script = sp.agent_script(task, ExecPath.CLIENT_SIDE, final)
    live, _ = _run(task, fault, list(script), Condition.GATE)
    shadow, _ = _run(task, fault, list(script), shadow=("crosscheck",))
    assert shadow.shadow["crosscheck"]["verdict"] == live.verdict
    assert shadow.shadow["crosscheck"]["adjudication"] == live.adjudication
    assert shadow.shadow["crosscheck"]["gate"]["failures"] == live.gate["failures"]


@pytest.mark.parametrize(
    ("kind", "fault", "contract_blocks"),
    [
        (tk.TaskKind.SPEC_LIMIT, FaultKind.F1, False),
        (tk.TaskKind.TEST_MEASURED, FaultKind.F3, False),
        (tk.TaskKind.SPEC_LIMIT, FaultKind.F2, True),
        (tk.TaskKind.SPEC_LIMIT, FaultKind.F6, True),
        (tk.TaskKind.DEFECT_COUNT, FaultKind.F5, True),
        (tk.TaskKind.WORK_ORDER, FaultKind.F4, True),
    ],
)
def test_contract_variant_coverage_and_crosscheck_catches_value_faults(
    kind, fault, contract_blocks
):
    task = sp.first_task(kind, fault)
    final = sp.truth_json(task)
    script = (
        _wo_script(task, final)
        if kind is tk.TaskKind.WORK_ORDER
        else sp.agent_script(task, ExecPath.CLIENT_SIDE, final)
    )
    rec, _ = _run(task, fault, script, shadow=SHADOW_VARIANTS)
    contract, cross = rec.shadow["contract"]["gate"], rec.shadow["crosscheck"]["gate"]
    assert contract["blocked"] is contract_blocks and contract["crosscheck"] is False
    assert cross["blocked"] is True and cross["crosscheck"] is True
    assert contract["quantity_checks"] == 0


def test_clean_runs_pass_both_shadows_unchanged():
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    final = sp.truth_json(task)
    rec, _ = _run(task, None, sp.agent_script(task, ExecPath.CLIENT_SIDE, final),
                  shadow=SHADOW_VARIANTS)  # fmt: skip
    for out in rec.shadow.values():
        assert out["gate"]["blocked"] is False
        assert out["adjudication"] == rec.adjudication


def test_a_run_error_carries_into_the_shadows():
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    rec, _ = _run(task, None, [XaiError("boom")], shadow=SHADOW_VARIANTS)
    assert rec.exclusion == "RUN_ERROR"
    assert all(out["adjudication"] is None for out in rec.shadow.values())


@pytest.mark.parametrize(
    ("condition", "path", "shadow", "error"),
    [
        (Condition.GATE, ExecPath.CLIENT_SIDE, ("contract",), GateUnavailable),
        (Condition.BASELINE, ExecPath.SERVER_SIDE, ("contract",), GateUnavailable),
        (Condition.BASELINE, ExecPath.CLIENT_SIDE, ("oracle",), ValueError),
        (Condition.BASELINE, ExecPath.CLIENT_SIDE, ("contract", "contract"), ValueError),
    ],
)
def test_shadow_gates_are_refused_where_they_cannot_be_scored(condition, path, shadow, error):
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    with pytest.raises(error):
        run_one(
            sp.plan(task, None), task, sp.MODEL, path, condition, ScriptedClient([]),
            run_id="r", clock=sp.CLOCK, shadow_gates=shadow,
        )  # fmt: skip


def test_an_isolated_side_block_leaves_the_agent_stream_where_it_was():
    store = tk.default_store()

    def injector():
        cfg = FaultConfig(11, (FaultSpec(FaultKind.F3, 0.5),))
        return FaultInjector(cfg, Ledger(None), "r", store, sp.CLOCK)

    a, b = injector(), injector()
    want = [a._rng.random() for _ in range(5)]
    with b.side_ledger(isolated=True):
        [b._rng.random() for _ in range(3)]
    assert [b._rng.random() for _ in range(5)] == want
    with b.side_ledger():  # the v1 default still shares the stream
        assert b._rng is b._agent_rng
