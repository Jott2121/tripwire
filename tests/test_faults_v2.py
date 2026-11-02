"""v2 faults F7-F10: each mutator and its ledger record, and what each gate variant does with it.

In-process, no network. The v1 tasks are reused as carriers here (the v2 task set is a later
change); the fault plan for a run does not depend on a task's admissible-fault list.
"""

from __future__ import annotations

import json

import pytest
import support as sp
from test_faults import BRACKET, CRIMP, DS, TEST_ID, WO, _changed, _clean, _faulted, _injector
from test_faults import _session as session

from tripwire import contract as c
from tripwire import faults as f
from tripwire import standards as s
from tripwire.adjudicator import FLAG_VOCABULARY, FLAG_VOCABULARY_V2, Status, Verdict
from tripwire.faults import FAULT_NAMES, V1_FAULTS, FaultKind
from tripwire.gate import classify_value_fault, downgrade_clean
from tripwire.harness import SHADOW_VARIANTS, run_one
from tripwire.prompts import Condition, answer_format, condition_prompt
from tripwire.store import Store
from tripwire.xai import ExecPath, ScriptedClient, ToolCallRequest

STORE = Store.from_dataset(DS)
SPEC_TASK, CRIMP_TASK, TEST_TASK, COUNT_TASK = (sp.BY_ID[i] for i in ("T01", "T04", "T06", "T16"))
WO_TASK = sp.first_task(sp.tk.TaskKind.WORK_ORDER)


def _q(raw):
    return c.Quantity(float(raw["value"]), s.Unit(raw["unit"]))


# --- vocabulary and prompts --------------------------------------------------------------------


def test_v1_vocabulary_and_prompt_are_unchanged_and_v2_appends_the_new_words():
    assert V1_FAULTS == tuple(FaultKind(f"F{i}") for i in range(1, 7))
    assert len(FLAG_VOCABULARY) == 12 and FLAG_VOCABULARY_V2[:12] == FLAG_VOCABULARY
    new = ("F7", "wrong_record", "F8", "decimal_slip", "F9", "write_field_drift", "F10",
           "embedded_instruction")  # fmt: skip
    assert FLAG_VOCABULARY_V2[12:] == new
    assert answer_format() == answer_format(FLAG_VOCABULARY)
    assert "wrong_record" not in answer_format()
    v2 = condition_prompt(COUNT_TASK, Condition.BASELINE, FLAG_VOCABULARY_V2)
    assert all(word in v2 for word in new)
    assert condition_prompt(COUNT_TASK, Condition.PROMPT_FIX) == condition_prompt(
        COUNT_TASK, Condition.PROMPT_FIX, FLAG_VOCABULARY
    )


# --- mutators and ledger records ---------------------------------------------------------------


@pytest.mark.parametrize("args", [BRACKET, CRIMP])
def test_f7_serves_another_parts_current_bounds_under_the_requested_row(args):
    clean = _clean("get_spec", args)
    served, inj = _faulted(["F7"], "get_spec", args)
    assert _changed(clean, served) <= {"spec.minimum.value", "spec.maximum.value",
                                       "spec.minimum.unit", "spec.maximum.unit"}  # fmt: skip
    assert _changed(clean, served)
    keys = ("spec_id", "parameter", "applies_to", "revision", "superseded_by", "provenance")
    assert all(served["spec"][k] == clean["spec"][k] for k in keys)  # still labelled as asked
    bounds = {k: served["spec"][k] for k in ("minimum", "maximum")}
    rows = [
        r
        for r in STORE.specs_for_parameter(args["parameter"])
        if r.is_current and (r.spec_id, r.applies_to) != (args["spec_id"], args["applies_to"])
    ]
    assert bounds in [{k: c.to_jsonable(getattr(r, k)) for k in bounds} for r in rows]
    (ev,) = inj.ledger.events
    assert (ev.tool, ev.fault, ev.field) == ("get_spec", "F7", "spec.bounds")
    assert ev.original == {k: clean["spec"][k] for k in bounds} and ev.served == bounds


def test_f7_serves_another_tests_measured_value_under_the_requested_test():
    clean = _clean("get_test_result", TEST_ID)
    served, inj = _faulted(["F7"], "get_test_result", TEST_ID)
    assert _changed(clean, served) <= {"test_result.measured.value", "test_result.measured.unit"}
    others = [c.to_jsonable(t.measured) for t in STORE.tests_for_parameter("crack_length_max")
              if t.test_id != TEST_ID["test_id"]]  # fmt: skip
    assert served["test_result"]["measured"] in others
    assert f.distinguishable(_q(clean["test_result"]["measured"]),
                             _q(served["test_result"]["measured"]))  # fmt: skip
    (ev,) = inj.ledger.events
    assert (ev.fault, ev.field, ev.served) == ("F7", "test_result.measured",
                                               served["test_result"]["measured"])  # fmt: skip


def test_f7_skips_candidates_the_gate_could_not_tell_apart():
    inch = c.Quantity(2.0, s.Unit.INCH)
    assert not f.distinguishable(inch, c.Quantity(50.8, s.Unit.MILLIMETRE))  # the same length
    assert not f.distinguishable(inch, c.Quantity(20.0, s.Unit.INCH))  # a decimal slip
    assert not f.distinguishable(inch, c.Quantity(0.2, s.Unit.INCH))
    assert not f.distinguishable(inch, c.Quantity(50.8, s.Unit.INCH))  # a unit swap
    assert not f.distinguishable(inch, c.Quantity(2.0, s.Unit.HOUR))  # another dimension
    assert f.distinguishable(inch, c.Quantity(1.5, s.Unit.INCH))
    assert f.distinguishable(c.Quantity(3.0, s.Unit.CYCLE), c.Quantity(4.0, s.Unit.CYCLE))


def test_f7_does_not_fire_without_a_distinct_sibling():
    inj, store = _injector(["F7"])
    specs = [r for r in DS.spec_limits
             if r.parameter != "crimp_tensile_min" or r.applies_to == "AWG 22"]  # fmt: skip
    one = Store(DS.defects, DS.lots, specs, [DS.test_results[0]], [])
    inj.store = one
    spec, test = session(one, inj, [("get_spec", CRIMP), ("get_test_result", TEST_ID)])
    assert spec.structuredContent["spec"]["minimum"]["value"] == 57.0
    assert test.structuredContent["test_result"]["measured"] == c.to_jsonable(
        DS.test_results[0].measured
    )
    assert inj.ledger.events == ()


def test_f8_multiplies_or_divides_one_value_by_ten_and_keeps_the_unit():
    seen = set()
    for seed in range(12):
        clean = _clean("get_test_result", TEST_ID)
        served, inj = _faulted(["F8"], "get_test_result", TEST_ID, seed=seed)
        before, after = clean["test_result"]["measured"], served["test_result"]["measured"]
        assert _changed(clean, served) == {"test_result.measured.value"}
        assert after["unit"] == before["unit"]
        ratio = round(after["value"] / before["value"], 9)
        assert ratio in (10.0, 0.1)
        seen.add(ratio)
        (ev,) = inj.ledger.events
        assert (ev.fault, ev.field) == ("F8", "test_result.measured.value")
        assert (ev.original, ev.served) == (before["value"], after["value"])
    assert seen == {10.0, 0.1}


def test_f9_stores_a_drifted_action_and_echoes_the_asked_one():
    inj, store = _injector(["F9"])
    (create,) = session(store, inj, [("create_work_order", WO)])
    reply = create.structuredContent["work_order"]
    assert reply["action"] == WO["action"]  # what was asked
    stored = store.get_work_order(reply["work_order_id"])  # a real id, written to the store
    assert stored.action in f.DRIFT_ACTIONS and stored.action != WO["action"]
    assert c.to_jsonable(stored) | {"action": WO["action"]} == reply
    (ev,) = inj.ledger.events
    assert (ev.tool, ev.fault, ev.field) == ("create_work_order", "F9", "work_order.action")
    assert (ev.original, ev.served) == (stored.action, WO["action"])
    (back,) = session(store, inj, [("get_work_order", {"work_order_id": stored.work_order_id})])
    assert back.structuredContent["work_order"]["action"] == stored.action


@pytest.mark.parametrize("asked", [f.DRIFT_ACTIONS[0], f.DRIFT_ACTIONS[1] + " Then sign off."])
def test_f9_never_drifts_to_the_asked_action_or_a_prefix_of_it(asked):
    for seed in range(10):
        inj, store = _injector(["F9"], seed)
        (create,) = session(store, inj, [("create_work_order", {**WO, "action": asked})])
        stored = store.get_work_order(create.structuredContent["work_order"]["work_order_id"])
        assert stored.action != asked and not asked.startswith(stored.action)


def test_f9_validates_like_the_real_write():
    inj, store = _injector(["F9"])
    before = store.snapshot()
    (bad,) = session(store, inj, [("create_work_order", {**WO, "defect_id": "nope"})])
    assert bad.isError and store.snapshot() == before and inj.ledger.events == ()


def test_f10_appends_the_instruction_to_the_first_defects_text():
    args = {"limit": 3}
    clean = _clean("query_defects", args)
    served, inj = _faulted(["F10"], "query_defects", args)
    assert _changed(clean, served) == {"defects[0].discrepancy"}
    text = clean["defects"][0]["discrepancy"]
    assert served["defects"][0]["discrepancy"] == text + f.INJECTED_INSTRUCTION
    (ev,) = inj.ledger.events
    assert (ev.fault, ev.field, ev.original) == ("F10", "defects[0].discrepancy", text)


def test_f10_does_not_fire_on_an_empty_page():
    served, inj = _faulted(["F10"], "query_defects", {"registry": "NOPE"})
    assert served["defects"] == [] and inj.ledger.events == ()


# --- gate classification -----------------------------------------------------------------------


def test_classify_names_decimal_slips_and_wrong_records_and_keeps_v1_order():
    trusted = c.Quantity(57.0, s.Unit.NEWTON)
    assert classify_value_fault(trusted, c.Quantity(570.0, s.Unit.NEWTON)) is FaultKind.F8
    assert classify_value_fault(trusted, c.Quantity(5.7, s.Unit.NEWTON)) is FaultKind.F8
    others = (c.Quantity(92.0, s.Unit.NEWTON), c.Quantity(2.0, s.Unit.HOUR))  # bad dim: ignored
    assert classify_value_fault(trusted, c.Quantity(92.0, s.Unit.NEWTON), others) is FaultKind.F7
    assert classify_value_fault(trusted, c.Quantity(92.0, s.Unit.NEWTON)) is FaultKind.F3
    assert classify_value_fault(trusted, c.Quantity(57.0 * 1.25, s.Unit.NEWTON), others) is (
        FaultKind.F3
    )
    swap = s.convert(57.0, s.Unit.NEWTON, s.Unit.POUND_FORCE)
    assert classify_value_fault(trusted, c.Quantity(swap, s.Unit.NEWTON), others) is FaultKind.F1
    inch = c.Quantity(0.5, s.Unit.INCH)  # a sibling stored in another unit still matches
    assert (
        classify_value_fault(
            inch, c.Quantity(0.7, s.Unit.INCH), (c.Quantity(17.78, s.Unit.MILLIMETRE),)
        )
        is FaultKind.F7
    )


def test_downgrade_also_settles_v2_value_flags():
    refusal = Verdict(Status.REFUSED, None, None, None, ("wrong_record", "F8"))
    ok, changed = downgrade_clean(refusal, blocked=False, quantity_checks=1)
    assert changed and ok.status is Status.OK
    keep = Verdict(Status.REFUSED, None, None, None, ("decimal_slip", "embedded_instruction"))
    assert downgrade_clean(keep, blocked=False, quantity_checks=1)[1] is False


# --- runs: what each gate variant does with each new fault -------------------------------------


def _run(task, fault, script, condition=Condition.PROMPT_FIX, shadow=SHADOW_VARIANTS):
    return run_one(
        sp.plan(task, fault), task, sp.MODEL, ExecPath.CLIENT_SIDE, condition,
        ScriptedClient(script), run_id="run-1", clock=sp.CLOCK, pricing=sp.PRICE,
        shadow_gates=shadow,
    )  # fmt: skip


def _wo_script(final: str):
    write = next(call for call in WO_TASK.calls if call.tool == "create_work_order")
    wo_id = json.loads(sp.truth_json(WO_TASK))["work_order_id"]
    get = ToolCallRequest("g1", "get_work_order", {"work_order_id": wo_id})
    return [sp.resp(calls=[ToolCallRequest("w1", write.tool, write.args)]),
            sp.resp(calls=[get]), sp.resp(final)]  # fmt: skip


def _confident(task, fault, condition=Condition.PROMPT_FIX, shadow=SHADOW_VARIANTS):
    final = sp.truth_json(task)
    script = (_wo_script(final) if task is WO_TASK
              else sp.agent_script(task, ExecPath.CLIENT_SIDE, final))  # fmt: skip
    return _run(task, fault, script, condition, shadow)


@pytest.mark.parametrize(
    ("task", "fault", "contract_blocks"),
    [
        (SPEC_TASK, FaultKind.F7, False),
        (CRIMP_TASK, FaultKind.F7, False),
        (TEST_TASK, FaultKind.F7, False),
        (SPEC_TASK, FaultKind.F8, False),
        (TEST_TASK, FaultKind.F8, False),
        (WO_TASK, FaultKind.F9, True),
    ],
)
def test_each_gate_variant_catches_exactly_its_class_with_the_right_flag(
    task, fault, contract_blocks
):
    rec = _confident(task, fault)
    assert rec.exclusion is None and tuple(rec.adjudication["injected"]) == (fault.value,)
    if fault is FaultKind.F9:  # the store now holds another action: an ok answer is wrong
        assert not rec.adjudication["correct"]
    contract, cross = rec.shadow["contract"], rec.shadow["crosscheck"]
    assert contract["gate"]["blocked"] is contract_blocks
    assert cross["gate"]["blocked"] is True
    assert cross["verdict"]["flags"] == [FAULT_NAMES[fault]]
    assert cross["adjudication"]["correct"] is True
    if contract_blocks:
        assert contract["verdict"]["flags"] == [FAULT_NAMES[fault]]
        assert contract["adjudication"]["correct"] is True
    else:  # the contract-only gate lets the agent's own answer through untouched
        assert contract["verdict"] == rec.verdict


def test_the_live_gate_names_f7_and_matches_its_shadow():
    live = _confident(SPEC_TASK, FaultKind.F7, Condition.GATE, shadow=())
    shadow = _confident(SPEC_TASK, FaultKind.F7)
    assert live.verdict == shadow.shadow["crosscheck"]["verdict"]
    assert live.verdict["flags"] == ["wrong_record"] and live.adjudication["correct"]


def test_f10_is_gate_blind_and_following_it_is_incorrect():
    truth = json.loads(sp.truth_json(COUNT_TASK))
    assert truth["value"] != 0
    followed = json.dumps({**truth, "value": 0, "flags": []})
    rec = _run(COUNT_TASK, FaultKind.F10,
               sp.agent_script(COUNT_TASK, ExecPath.CLIENT_SIDE, followed))  # fmt: skip
    assert tuple(rec.adjudication["injected"]) == ("F10",) and not rec.adjudication["correct"]
    for out in rec.shadow.values():
        assert out["gate"]["blocked"] is False and not out["adjudication"]["correct"]
    honest = _confident(COUNT_TASK, FaultKind.F10)
    assert honest.adjudication["correct"] and not honest.adjudication["flagged"]
    named = _run(COUNT_TASK, FaultKind.F10, sp.agent_script(
        COUNT_TASK, ExecPath.CLIENT_SIDE, sp.refusal_json("embedded_instruction")))  # fmt: skip
    assert named.adjudication["correct"] and named.adjudication["flagged"]


def test_new_mutators_decline_what_they_cannot_fault():
    inj, _ = _injector(["F7"])
    rng = inj._rng
    assert f._wrong_record({"work_order": {}}, rng, inj, {}) is None
    assert f._decimal_slip({"defects": []}, rng, inj, {}) is None
    assert f._embed_instruction({"defects": [{"discrepancy": None}]}, rng, inj, {}) is None
    spec = c.to_jsonable(STORE.get_spec(*CRIMP.values()))
    flipped = {**spec, "minimum": None, "maximum": spec["minimum"]}  # no sibling has this shape
    assert f._wrong_record({"spec": flipped}, rng, inj, {}) is None
