"""The gate (M6): each fault vs gate on/off, clean runs, read-back, and fail-closed errors."""

from __future__ import annotations

import json

import pytest
import support as sp

from tripwire import contract, xai
from tripwire import standards as s
from tripwire import tasks as tk
from tripwire.adjudicator import FLAG_VOCABULARY, Status, Verdict
from tripwire.faults import FAULT_NAMES, FaultKind
from tripwire.gate import Failure, Gate, classify_value_fault, downgrade_clean
from tripwire.harness import run_one
from tripwire.prompts import Condition
from tripwire.xai import ExecPath


def _run(task, fault, client, condition=Condition.GATE, **kw):
    return run_one(
        sp.plan(task, fault), task, sp.MODEL, ExecPath.CLIENT_SIDE, condition, client,
        run_id="run-1", clock=sp.CLOCK, pricing=sp.PRICE, **kw,
    )  # fmt: skip


def _confident(task, fault, condition):
    """An agent that reads what the task needs and answers with the true answer, no flags."""
    script = sp.agent_script(task, ExecPath.CLIENT_SIDE, sp.truth_json(task))
    return _run(task, fault, sp.client_for(script), condition)


CAUGHT = [FaultKind.F2, FaultKind.F4, FaultKind.F5, FaultKind.F6]
KIND = {
    FaultKind.F2: tk.TaskKind.SPEC_LIMIT,
    FaultKind.F4: tk.TaskKind.WORK_ORDER,
    FaultKind.F5: tk.TaskKind.DEFECT_COUNT,
    FaultKind.F6: tk.TaskKind.SPEC_LIMIT,
}
NOT_CAUGHT = [FaultKind.F1, FaultKind.F3]


@pytest.mark.parametrize("fault", CAUGHT)
def test_gate_blocks_and_names_each_catchable_fault(fault):
    task = sp.first_task(KIND[fault], fault)
    rec = _confident(task, fault, Condition.GATE)
    assert rec.exclusion is None
    assert rec.gate["blocked"] and rec.verdict["status"] == "blocked"
    assert rec.verdict["flags"] == [FAULT_NAMES[fault]] and set(rec.verdict["flags"]) <= set(
        FLAG_VOCABULARY
    )
    assert rec.adjudication["flagged"] and rec.adjudication["correct"]
    assert rec.gate["agent_verdict"]["status"] == "ok"  # what the agent would have said


def test_f4_without_the_gate_the_confident_agent_is_wrong():
    task = sp.first_task(tk.TaskKind.WORK_ORDER, FaultKind.F4)
    off = _confident(task, FaultKind.F4, Condition.PROMPT_FIX)
    on = _confident(task, FaultKind.F4, Condition.GATE)
    assert off.gate is None and off.adjudication["correct"] is False
    assert on.adjudication["correct"] is True


@pytest.mark.parametrize("fault", [FaultKind.F2, FaultKind.F5, FaultKind.F6])
def test_without_the_gate_the_same_agent_is_never_blocked(fault):
    task = sp.first_task(KIND[fault], fault)
    off = _confident(task, fault, Condition.PROMPT_FIX)
    assert off.gate is None and off.verdict["status"] == "ok"


@pytest.mark.parametrize("fault", NOT_CAUGHT)
@pytest.mark.parametrize("kind", [tk.TaskKind.SPEC_LIMIT, tk.TaskKind.TEST_MEASURED,
                                  tk.TaskKind.DEFECT_CRACK])  # fmt: skip
def test_gate_now_catches_f1_and_f3_by_store_crosscheck(kind, fault):
    """Issue #44: a served value that disagrees with the store is blocked and named, where the
    old gate passed F1/F3 (a valid label and a plausible number have nothing in the metadata)."""
    task = sp.first_task(kind, fault)
    rec = _confident(task, fault, Condition.GATE)
    assert rec.exclusion is None and rec.gate["blocked"]
    assert rec.gate["quantity_checks"] >= 1
    assert rec.verdict["status"] == "blocked" and rec.verdict["flags"] == [FAULT_NAMES[fault]]
    assert rec.adjudication["flagged"] and rec.adjudication["correct"]


def test_classify_value_fault_separates_unit_swap_from_wrong_value():
    """F1 is the trusted value in a swapped unit (label unchanged); any other gap is F3. The
    swap ratios come from the defined factors, disjoint from the fault middleware's 0.8..1.25."""
    for (src, dst), factor in s.CONVERSION_FACTORS.items():
        base = contract.Quantity(2.5, src)
        assert classify_value_fault(base, base) is None
        swapped = contract.Quantity(2.5 * factor, src)  # converted value, label left on src
        assert classify_value_fault(base, swapped) is FaultKind.F1
        back = contract.Quantity(2.5, dst)
        assert classify_value_fault(back, contract.Quantity(2.5 / factor, dst)) is FaultKind.F1
    length = contract.Quantity(2.0, s.Unit.INCH)
    for wrong in (0.8, 0.9, 1.1, 1.25):
        assert classify_value_fault(length, contract.Quantity(2.0 * wrong, s.Unit.INCH)) is (
            FaultKind.F3
        )
    cycles = contract.Quantity(100.0, s.Unit.CYCLE)  # not swappable: any change is wrong_value
    assert classify_value_fault(cycles, contract.Quantity(125.0, s.Unit.CYCLE)) is FaultKind.F3
    assert classify_value_fault(length, contract.Quantity(2.0, s.Unit.HOUR)) is FaultKind.F1


def _refusal(*flags):
    return Verdict(Status.REFUSED, None, None, None, tuple(flags))


def test_downgrade_clean_only_fires_with_positive_evidence_and_value_flags():
    ok, changed = downgrade_clean(_refusal("F1"), blocked=False, quantity_checks=1)
    assert changed and ok.status is Status.OK and ok.flags == ()
    # no positive evidence (verified no quantity): leave the refusal alone
    assert downgrade_clean(_refusal("F1"), blocked=False, quantity_checks=0) == (
        _refusal("F1"),
        False,
    )
    # blocked run: never downgraded (the block stands)
    assert downgrade_clean(_refusal("F3"), blocked=True, quantity_checks=3)[1] is False
    # a non-value flag the cross-check cannot disprove: leave it
    assert downgrade_clean(_refusal("F1", "F5"), blocked=False, quantity_checks=2)[1] is False
    # not a refusal, or no flags: nothing to downgrade
    assert (
        downgrade_clean(Verdict(Status.OK, 1.0, s.Unit.INCH), blocked=False, quantity_checks=1)[1]
        is False
    )
    assert downgrade_clean(_refusal(), blocked=False, quantity_checks=1)[1] is False


@pytest.mark.parametrize("kind", [k for k in tk.TaskKind])
def test_clean_runs_pass_the_gate_with_no_false_flag(kind):
    task = sp.first_task(kind)
    rec = _confident(task, None, Condition.GATE)
    assert rec.gate["blocked"] is False and rec.gate["failures"] == []
    assert rec.gate["checks_run"] >= 1
    assert rec.adjudication["correct"] and not rec.adjudication["false_flag"]


def test_gate_cost_is_recorded_for_a_write_task():
    task = sp.first_task(tk.TaskKind.WORK_ORDER)
    rec = _confident(task, None, Condition.GATE)
    assert rec.gate["extra_tool_calls"] == 1 and rec.gate["added_tokens"] == 0
    assert rec.gate["checks_run"] == 2 and rec.gate["latency_s"] >= 0
    assert rec.tool_calls == ("create_work_order",)  # the read-back is not the agent's call
    assert json.loads(rec.to_json())["gate"]["blocked"] is False


def test_gate_downgrades_a_clean_runs_unsupported_value_refusal():
    """Issue #44: on data the gate verified against the store, an agent's bare F1/F3 refusal is
    rewritten to its answer with the unsupported flag dropped, so it is no longer a false flag."""
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    script = sp.agent_script(task, ExecPath.CLIENT_SIDE, sp.refusal_json("F1"))
    rec = _run(task, None, sp.client_for(script))
    assert rec.gate["blocked"] is False and rec.gate["downgraded"] is True
    assert rec.verdict["status"] == "ok" and rec.verdict["flags"] == []
    assert not rec.adjudication["false_flag"]
    assert (
        rec.gate["agent_verdict"]["status"] == "refused"
    )  # the pre-gate verdict is on the receipt


def test_without_the_gate_a_clean_run_value_refusal_is_still_a_false_flag():
    """The downgrade is the gate's doing: prompt_fix alone keeps the false flag."""
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    script = sp.agent_script(task, ExecPath.CLIENT_SIDE, sp.refusal_json("F1"))
    rec = _run(task, None, sp.client_for(script), condition=Condition.PROMPT_FIX)
    assert rec.gate is None and rec.verdict["flags"] == ["F1"]
    assert rec.adjudication["false_flag"]


def test_gate_will_not_downgrade_a_refusal_it_cannot_disprove():
    """A refusal that names a non-value fault, or that read no quantity, is left untouched."""
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    other = sp.agent_script(task, ExecPath.CLIENT_SIDE, sp.refusal_json("F5", "F1"))
    rec = _run(task, None, sp.client_for(other))
    assert rec.gate["downgraded"] is False and rec.adjudication["false_flag"]
    # a refusal with no tool reads: no positive store evidence, so no downgrade
    bare = [sp.resp(sp.refusal_json("F1"))]
    rec2 = _run(task, None, sp.client_for(bare))
    assert rec2.gate["quantity_checks"] == 0 and rec2.gate["downgraded"] is False
    assert rec2.adjudication["false_flag"]


def test_gate_internal_error_fails_closed(monkeypatch):
    def boom(self, tool, args, result):
        raise RuntimeError("gate bug")

    monkeypatch.setattr(Gate, "_contract", boom)
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    rec = _confident(task, None, Condition.GATE)
    assert rec.gate["blocked"] and rec.verdict["status"] == "blocked"
    assert rec.gate["failures"][0]["check"] == "gate_error" and rec.verdict["flags"] == []
    assert rec.adjudication["false_flag"]  # a clean-run block is a false flag, and is counted


def test_turn_cap_and_run_error_are_not_masked_by_the_gate():
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    rec = _run(task, None, xai.ScriptedClient([xai.XaiError("x")]))
    assert rec.exclusion == "RUN_ERROR" and rec.gate["blocked"] is False
    loop = [sp.resp(calls=sp.read_calls(task)) for _ in range(12)]
    assert _run(task, None, sp.client_for(loop)).turn_cap_hit


def _gate():
    return Gate(tk.default_store())


WO = {"work_order_id": "WO-000001", "defect_id": "d", "action": "replace the clamp",
      "created_by": "agent-01", "status": "open", "created_at": "2026-10-11T12:00:00+00:00",
      "provenance": {"source": "x", "source_url": "u", "revision": "r",
                     "retrieved_at": "2026-10-11T12:00:00+00:00"}}  # fmt: skip
ARGS = {"defect_id": "d", "action": "replace the clamp", "created_by": "agent-01"}


def _verify(back):
    g = _gate()
    g.verify_write(ARGS, {"work_order": WO}, back)
    return g


def test_read_back_passes_on_an_exact_match():
    g = _verify({"work_order": dict(WO)})
    assert not g.blocked and g.checks_run == 1


def test_read_back_missing_record_is_f4():
    g = _verify({"error": "NOT_FOUND: no work order 'WO-000001'"})
    assert g.failures[0] == Failure("read_back_missing", FaultKind.F4, g.failures[0].detail)
    assert g.flags() == ("silent_noop_write",)


def test_read_back_mismatch_and_truncation_and_dropped_field():
    other = _verify({"work_order": {**WO, "defect_id": "z"}})
    assert other.failures[0].check == "read_back_mismatch" and other.flags() == ()
    cut = _verify({"work_order": {**WO, "action": "replace"}})
    assert cut.flags() == ("silent_truncation",)
    differs = _verify({"work_order": {**WO, "action": "something else"}})
    assert differs.blocked and differs.flags() == ()
    dropped = {k: v for k, v in WO.items() if k != "provenance"}
    assert _verify({"work_order": dropped}).flags() == ("dropped_field",)


def test_a_failed_write_is_not_read_back_and_a_bad_readback_shape_blocks():
    g = _gate()
    g.verify_write(ARGS, {"error": "VALIDATION: no defect"}, {"error": "x"})
    assert not g.blocked
    assert _verify({"work_order": None}).blocked


def test_contract_rejects_bad_units_provenance_and_shapes():
    g = _gate()
    store = tk.default_store()
    spec = tk.default_tasks()[0]
    assert spec  # tasks exist
    d = json.loads(json.dumps(_defect(store)))
    bad_unit = {**d, "crack_length": {"value": 1.0, "unit": "furlong"}}
    wrong_dim = {**d, "crack_length": {"value": 1.0, "unit": "h"}}
    bad_value = {**d, "crack_length": {"value": "x", "unit": "in"}}
    odd_shape = {**d, "crack_length": {"value": 1.0}}
    weak_prov = {**d, "provenance": {**d["provenance"], "retrieved_at": "2026-10-11T12:00:00"}}
    assert {
        "unit_invalid",
        "unit_dimension",
        "quantity_value",
        "quantity_shape",
        "provenance_malformed",
    } == {  # fmt: skip
        _check_defect(g, row) for row in (bad_unit, wrong_dim, bad_value, odd_shape, weak_prov)
    }
    assert _check_defect(_gate(), None) == "record_shape"
    g2 = _gate()
    g2.observe("query_defects", {}, {"defects": "no"})
    g2.observe("nope", {}, {})
    g2.observe("get_work_order", {}, {"error": "NOT_FOUND: x"})  # a typed error is data
    assert [f.check for f in g2.failures] == ["result_shape", "unknown_tool"]


def _defect(store):
    from tripwire import contract as c

    return c.to_jsonable(store.query_defects(limit=1).defects[0])


def _check_defect(gate, row):
    gate.observe("query_defects", {"limit": 1}, {"defects": [row], "total": 1, "truncated": False})
    return gate.failures[-1].check


def test_count_check_uses_total_limit_and_truncated():
    g = _gate()
    row = _defect(tk.default_store())
    g.observe("query_defects", {"limit": 2}, {"defects": [row], "total": 5, "truncated": True})
    assert g.failures[0].check == "count_mismatch" and g.flags() == ("silent_truncation",)
    ok = _gate()
    ok.observe("query_defects", {}, {"defects": [row], "total": 1, "truncated": False})
    ok.observe("query_defects", {"limit": 1}, {"defects": [row], "total": 3, "truncated": True})
    assert not ok.blocked


def test_read_back_faults_go_to_the_receipt_not_the_run_ledger():
    """F5 only fires on get_work_order for a write task: the agent never saw it."""
    task = sp.first_task(tk.TaskKind.WORK_ORDER)
    rec = _confident(task, FaultKind.F5, Condition.GATE)
    faults = rec.gate["read_back_faults"]
    assert [f["fault"] for f in faults] == ["F5"] and faults[0]["tool"] == "get_work_order"
    assert rec.adjudication["injected"] == ()  # the run ledger holds nothing the agent saw
    assert rec.exclusion == "INJECTION_UNVERIFIED"  # the gate's own call cannot verify a plan
    assert rec.gate["blocked"]  # the gate still blocks on what the read-back saw


def test_a_fault_on_the_agents_own_write_is_still_ledgered_once():
    task = sp.first_task(tk.TaskKind.WORK_ORDER)
    rec = _confident(task, FaultKind.F6, Condition.GATE)
    assert rec.adjudication["injected"] == ("F6",)  # the agent's create_work_order call
    assert rec.exclusion is None and rec.gate["blocked"]


def test_clean_run_has_no_read_back_faults():
    task = sp.first_task(tk.TaskKind.WORK_ORDER)
    assert _confident(task, None, Condition.GATE).gate["read_back_faults"] == []


def test_spec_with_neither_bound_is_a_contract_failure_not_a_gate_error():
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    store = tk.default_store()
    call = next(c for c in task.calls if c.tool == "get_spec")
    good = store.get_spec(call.args["spec_id"], call.args["parameter"], call.args["applies_to"])
    spec = contract.to_jsonable(good)
    spec["minimum"] = spec["maximum"] = None
    g = Gate(store)
    g.observe("get_spec", call.args, {"spec": spec})
    assert g.blocked and [f.check for f in g.failures] == ["spec_unbounded"]


@pytest.mark.parametrize("fault", [FaultKind.F2, FaultKind.F6])
def test_a3_turn_cap_after_a_gate_block_records_the_blocked_report(fault):
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    loop = [sp.resp(calls=sp.read_calls(task)) for _ in range(12)]
    rec = _run(task, fault, sp.client_for(loop))
    assert rec.turn_cap_hit and rec.gate["blocked"] and rec.exclusion is None
    assert rec.verdict["status"] == "blocked" and FAULT_NAMES[fault] in rec.verdict["flags"]
    assert rec.adjudication["correct"] is True


def test_a3_turn_cap_without_a_gate_block_still_has_no_verdict():
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    loop = [sp.resp(calls=sp.read_calls(task)) for _ in range(12)]
    rec = _run(task, None, sp.client_for(loop))
    assert rec.turn_cap_hit and not rec.gate["blocked"] and rec.verdict is None
    assert rec.adjudication["correct"] is False


def test_a3_does_not_touch_ungated_turn_caps():
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    loop = [sp.resp(calls=sp.read_calls(task)) for _ in range(12)]
    rec = _run(task, FaultKind.F6, sp.client_for(loop), Condition.PROMPT_FIX)
    assert rec.turn_cap_hit and rec.gate is None and rec.verdict is None
    assert rec.adjudication["correct"] is False
