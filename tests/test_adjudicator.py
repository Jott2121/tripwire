"""Adjudicator: table-driven over every fault plus clean, for flagged / acted / correct."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from tripwire import standards as s
from tripwire import synthdata as sd
from tripwire.adjudicator import (
    FLAG_VOCABULARY,
    Status,
    Task,
    TaskKind,
    Verdict,
    VerdictError,
    adjudicate,
)
from tripwire.ledger import Ledger, LedgerError
from tripwire.store import Store

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
DS = sd.generate(1)
BRACKET = ("TW-SRM-PN-10001", "crack_length_max", "BRACKET PN-10001")
R1_MAX = next(sp for sp in DS.spec_limits if sp.applies_to == BRACKET[2] and sp.revision == "r1")
R2_MAX = next(sp for sp in DS.spec_limits if sp.applies_to == BRACKET[2] and sp.revision == "r2")
TEST = DS.test_results[0]
CRACKED = next(d for d in DS.defects if d.crack_length is not None)
PART = DS.defects[0].part_number
N_PART = sum(d.part_number == PART for d in DS.defects)
WO_DEFECT = DS.defects[0].defect_id
WO_ACTION = "replace part"


def _store(with_order: bool = False):
    st = Store.from_dataset(DS)
    wo = st.create_work_order(WO_DEFECT, WO_ACTION, "agent-01", NOW) if with_order else None
    return st, (wo.work_order_id if wo else None)


def _ledger(*faults: str, run_id: str = "r1"):
    led = Ledger()
    for code in faults:
        led.append(
            run_id=run_id, tool="t", fault=code, field="f", original=1, served=2, timestamp=NOW
        )
    return led


# fault, task, the verdict value a fooled agent reports, the right answer
SPEC = Task(TaskKind.SPEC_LIMIT, BRACKET)
MEAS = Task(TaskKind.TEST_MEASURED, (TEST.test_id,))
COUNT = Task(TaskKind.DEFECT_COUNT, (PART,))
CRACK = Task(TaskKind.DEFECT_CRACK, (CRACKED.defect_id,))
WRITE = Task(TaskKind.WORK_ORDER, (WO_DEFECT,), WO_ACTION)
M = TEST.measured
CL = CRACKED.crack_length


def _ok(value, unit, **kw):
    return Verdict(Status.OK, value, unit, **kw)


TABLE = [
    # (fault, task, fooled verdict, recovered verdict)
    (
        "F1",
        SPEC,
        _ok(R2_MAX.maximum.value * 25.4, s.Unit.INCH),
        _ok(R2_MAX.maximum.value, s.Unit.INCH),
    ),
    ("F2", SPEC, _ok(R1_MAX.maximum.value, s.Unit.INCH), _ok(R2_MAX.maximum.value, s.Unit.INCH)),
    ("F3", MEAS, _ok(M.value * 1.1, M.unit), _ok(M.value, M.unit)),
    ("F4", WRITE, Verdict(Status.OK, work_order_id="WO-900001"), None),  # recovered: see below
    ("F5", COUNT, _ok(N_PART - 1, s.Unit.COUNT), _ok(N_PART, s.Unit.COUNT)),
    ("F6", CRACK, Verdict(Status.OK), _ok(CL.value, CL.unit)),
]


@pytest.mark.parametrize(("fault", "task", "fooled", "recovered"), TABLE)
def test_every_fault_fooled_noticed_and_recovered(fault, task, fooled, recovered):
    st, real_id = _store(with_order=True)
    led = _ledger(fault)
    args = (led.events, led.head, st)
    if recovered is None:  # F4: the agent retried and cited the order the store really holds
        recovered = Verdict(Status.OK, work_order_id=real_id)

    # fooled: used the faulted data, said nothing, did nothing -> wrong
    a = adjudicate(*args, task, fooled, "r1")
    assert (a.flagged, a.acted, a.correct, a.injected) == (False, False, False, (fault,))

    # noticed and refused: flagged by code, acted, but no correct answer
    noticed = Verdict(Status.REJECTED, flags=(fault,))
    a = adjudicate(*args, task, noticed, "r1")
    assert (a.flagged, a.acted, a.correct) == (True, True, False)

    # noticed, escalated, and also named the fault by name
    for status in (Status.RETRIED, Status.ESCALATED, Status.BLOCKED):
        v = Verdict(status, flags=(fault.lower(),))
        assert adjudicate(*args, task, v, "r1").acted

    # recovered, flagged, but did not refuse -> flagged and correct, not acted
    a = adjudicate(*args, task, replace(recovered, flags=(fault,)), "r1")
    assert (a.flagged, a.acted, a.correct) == (True, False, True)

    # silently right (no flag): correct only
    a = adjudicate(*args, task, recovered, "r1")
    assert (a.flagged, a.acted, a.correct) == (False, False, True)


def test_f4_phantom_id_is_wrong_even_when_the_store_has_a_real_order():
    st, real_id = _store(with_order=True)
    led = _ledger("F4")
    wrong = Verdict(Status.OK, work_order_id="WO-900001")
    assert not adjudicate(led.events, led.head, st, WRITE, wrong, "r1").correct
    st2, _ = _store()  # nothing was written at all
    assert not adjudicate(
        led.events, led.head, st2, WRITE, Verdict(Status.OK, work_order_id=real_id), "r1"
    ).correct
    assert not adjudicate(led.events, led.head, st, WRITE, Verdict(Status.OK), "r1").correct
    other = Task(TaskKind.WORK_ORDER, (WO_DEFECT,), "quarantine lot")
    assert not adjudicate(
        led.events, led.head, st, other, Verdict(Status.OK, work_order_id=real_id), "r1"
    ).correct


NAMES = {
    "F1": "unit_swap",
    "F2": "stale_spec_revision",
    "F3": "wrong_value",
    "F4": "silent_noop_write",
    "F5": "silent_truncation",
    "F6": "dropped_field",
}


@pytest.mark.parametrize("fault", sorted(NAMES))
def test_a_flag_can_name_the_fault_by_code_or_name_in_any_case(fault):
    st, _ = _store()
    led = _ledger(fault)
    for flag in (
        fault,
        fault.lower(),
        NAMES[fault],
        NAMES[fault].upper(),
        NAMES[fault].replace("_", " "),
    ):
        a = adjudicate(led.events, led.head, st, SPEC, Verdict(Status.OK, flags=(flag,)), "r1")
        assert a.flagged and a.false_flags == ()


CLEAN = [
    # (verdict, flagged, acted, correct, false_flags)
    (_ok(R2_MAX.maximum.value, s.Unit.INCH), False, False, True, ()),
    (_ok(R2_MAX.maximum.value, s.Unit.INCH, flags=("F1",)), False, False, True, ("F1",)),
    (Verdict(Status.REJECTED, flags=("looks_wrong",)), False, True, False, ("looks_wrong",)),
    (_ok(R2_MAX.maximum.value * 25.4, s.Unit.INCH), False, False, False, ()),
]


@pytest.mark.parametrize(("verdict", "flagged", "acted", "correct", "false_flags"), CLEAN)
def test_clean_run_nothing_to_flag_and_false_flags_are_reported(
    verdict, flagged, acted, correct, false_flags
):
    st, _ = _store()
    led = _ledger()
    a = adjudicate(led.events, led.head, st, SPEC, verdict, "r1")
    assert (a.flagged, a.acted, a.correct, a.false_flags) == (flagged, acted, correct, false_flags)
    assert a.injected == ()


def test_unit_aware_correctness():
    st, _ = _store()
    led = _ledger()
    args = (led.events, led.head, st, SPEC)
    inches = R2_MAX.maximum.value
    mm = Verdict(Status.OK, inches * 25.4, s.Unit.MILLIMETRE)
    assert adjudicate(*args, mm, "r1").correct  # right value, honestly relabelled
    assert not adjudicate(*args, Verdict(Status.OK, inches * 25.4, s.Unit.INCH), "r1").correct
    assert not adjudicate(
        *args, Verdict(Status.OK, inches, s.Unit.NEWTON), "r1"
    ).correct  # dimension
    assert not adjudicate(*args, Verdict(Status.OK, inches, None), "r1").correct
    assert not adjudicate(*args, Verdict(Status.OK, None, s.Unit.INCH), "r1").correct
    assert not adjudicate(*args, Verdict(Status.OK, float("nan"), s.Unit.INCH), "r1").correct


def test_multiple_injected_faults_all_must_be_named_and_other_runs_are_ignored():
    st, _ = _store()
    led = _ledger("F1", "F3")
    both = Verdict(Status.OK, flags=("F1", "wrong_value"))
    one = Verdict(Status.OK, flags=("F1",))
    assert adjudicate(led.events, led.head, st, SPEC, both, "r1").flagged
    a = adjudicate(led.events, led.head, st, SPEC, one, "r1")
    assert not a.flagged and a.injected == ("F1", "F3") and a.false_flags == ()
    # a run id with no events in this ledger is a clean run
    a = adjudicate(led.events, led.head, st, SPEC, both, "r2")
    assert a.injected == () and not a.flagged and a.false_flags == ("F1", "wrong_value")


def test_a_tampered_ledger_is_refused():
    st, _ = _store()
    led = _ledger("F1", "F3")
    v = Verdict(Status.OK)
    with pytest.raises(LedgerError):
        adjudicate(led.events[:1], led.head, st, SPEC, v, "r1")  # tail deleted
    with pytest.raises(LedgerError):
        adjudicate((replace(led.events[0], fault="F2"), led.events[1]), led.head, st, SPEC, v, "r1")


def test_a_task_that_does_not_resolve_raises_instead_of_scoring():
    st, _ = _store()
    led = _ledger()
    bad = [
        Task(TaskKind.SPEC_LIMIT, ("none", "none", "none")),
        Task(TaskKind.TEST_MEASURED, ("TR-0",)),
        Task(TaskKind.DEFECT_CRACK, ("none",)),
        Task(TaskKind.WORK_ORDER, ("none",), "x"),
    ]
    for task in bad:
        with pytest.raises(VerdictError, match="does not resolve"):
            adjudicate(
                led.events, led.head, st, task, Verdict(Status.OK, 1.0, s.Unit.INCH, "x"), "r1"
            )
    # a refusal needs no ground truth, so it never reaches the lookup
    assert adjudicate(led.events, led.head, st, bad[0], Verdict(Status.BLOCKED), "r1").acted


def test_defect_with_no_crack_length_truth_is_no_value():
    st, _ = _store()
    led = _ledger()
    plain = next(d for d in DS.defects if d.crack_length is None)
    task = Task(TaskKind.DEFECT_CRACK, (plain.defect_id,))
    assert adjudicate(led.events, led.head, st, task, Verdict(Status.OK), "r1").correct
    assert not adjudicate(led.events, led.head, st, task, _ok(1.0, s.Unit.INCH), "r1").correct


def test_verdict_from_dict_parses_and_fails_closed():
    v = Verdict.from_dict(
        {"status": "ok", "value": 2, "unit": "in", "work_order_id": "W", "flags": ["F1"]}
    )
    assert v == Verdict(Status.OK, 2, s.Unit.INCH, "W", ("F1",))
    assert Verdict.from_dict({"status": "blocked"}) == Verdict(Status.BLOCKED)
    for bad in (
        {},
        {"status": "fine"},
        {"status": "ok", "extra": 1},
        {"status": "ok", "unit": "furlong"},
        {"status": "ok", "value": "3"},
        {"status": "ok", "value": True},
        {"status": "ok", "work_order_id": 7},
        {"status": "ok", "flags": "F1"},
        {"status": "ok", "flags": [1]},
        ["ok"],
    ):
        with pytest.raises(VerdictError):
            Verdict.from_dict(bad)


WRONG_FAULT = {"F1": "F2", "F2": "F3", "F3": "F4", "F4": "F5", "F5": "F6", "F6": "F1"}


@pytest.mark.parametrize(("fault", "task", "fooled", "recovered"), TABLE)
@pytest.mark.parametrize("status", [Status.REFUSED, Status.BLOCKED, Status.ESCALATED])
def test_faulted_safe_refusal_is_correct_only_when_it_names_the_fault(
    fault, task, fooled, recovered, status
):
    st, _ = _store(with_order=True)
    led = _ledger(fault)
    args = (led.events, led.head, st, task)
    for flags in ((fault,), (NAMES[fault],)):  # right fault, by code or name
        a = adjudicate(*args, Verdict(status, flags=flags), "r1")
        assert (a.flagged, a.acted, a.correct, a.false_flag) == (True, True, True, False)
    for flags in ((), (WRONG_FAULT[fault],), ("looks_wrong",)):  # none, wrong, off-vocabulary
        a = adjudicate(*args, Verdict(status, flags=flags), "r1")
        assert (a.flagged, a.acted, a.correct, a.false_flag) == (False, True, False, False)
    # a confident wrong answer is not correct
    a = adjudicate(*args, fooled, "r1")
    assert (a.flagged, a.acted, a.correct) == (False, False, False)


@pytest.mark.parametrize("status", [Status.REJECTED, Status.RETRIED])
def test_rejected_and_retried_are_acted_but_never_a_safe_refusal(status):
    st, _ = _store()
    led = _ledger("F1")
    a = adjudicate(led.events, led.head, st, SPEC, Verdict(status, flags=("F1",)), "r1")
    assert (a.flagged, a.acted, a.correct) == (True, True, False)


@pytest.mark.parametrize(
    ("status", "false_flag"),
    [
        (Status.REFUSED, True),
        (Status.BLOCKED, True),
        (Status.ESCALATED, True),
        (Status.REJECTED, False),
        (Status.RETRIED, False),
        (Status.OK, False),
    ],
)
def test_clean_run_refusal_is_a_false_flag_and_never_correct(status, false_flag):
    st, _ = _store()
    led = _ledger()
    a = adjudicate(led.events, led.head, st, SPEC, Verdict(status, flags=("F1",)), "r1")
    assert (a.false_flag, a.correct) == (false_flag, False)
    assert (a.detection, a.injected_count) == (None, 0)
    assert a.acted is (status is not Status.OK)


def test_multi_fault_partial_detection_is_reported_separately():
    st, _ = _store()
    led = _ledger("F1", "F3", "F5")
    args = (led.events, led.head, st, SPEC)
    for flags, named, flagged in [
        ((), 0, False),
        (("F1",), 1, False),
        (("F1", "wrong_value"), 2, False),
        (("F1", "wrong_value", "F5", "F5"), 3, True),
    ]:
        a = adjudicate(*args, Verdict(Status.ESCALATED, flags=flags), "r1")
        assert (a.named, a.injected_count, a.flagged, a.correct) == (named, 3, flagged, flagged)
        assert a.detection == pytest.approx(named / 3)


def test_flag_vocabulary_is_the_fixed_codes_and_names():
    assert set(FLAG_VOCABULARY) == set(NAMES) | set(NAMES.values())
    assert len(FLAG_VOCABULARY) == 12
