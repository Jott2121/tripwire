from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from tripwire.corrupt import (
    Corruption,
    InjectionError,
    inject,
    verify_ledger,
    write_ledger,
)
from tripwire.sdr import SDRRecord, load_sdr_csv
from tripwire.synth import make_sdr_rows, write_sdr_csv

EASTERN = timezone(timedelta(hours=-5))


def rec(
    cn,
    *,
    part_name="BRACKET",
    part_number="PN100",
    crack=None,
    total_time=None,
    total_cycles=None,
    discrepancy="short",
    difficulty=date(2026, 1, 10),
    submission=datetime(2026, 1, 11, 9, 0, tzinfo=EASTERN),
):
    return SDRRecord(
        control_number=cn,
        difficulty_date=difficulty,
        submission_date=submission,
        jasc_code="2820",
        aircraft_make="ACME AERO",
        aircraft_model="AC-200",
        aircraft_total_time_hours=total_time,
        aircraft_total_cycles=total_cycles,
        part_name=part_name,
        part_number=part_number,
        part_condition="CRACKED",
        crack_length_in=crack,
        discrepancy=discrepancy,
    )


def _differing_indices(before, after):
    """Indices present in both lists whose record changed, plus any appended."""
    changed = [i for i in range(len(before)) if before[i] != after[i]]
    appended = list(range(len(before), len(after)))
    return changed, appended


def test_unit_swap_changes_only_the_logged_field():
    records = [rec("A", crack=2.0)]
    before = list(records)
    result = inject(records, {Corruption.UNIT_SWAP: 1}, seed=1)
    (event,) = result.ledger
    assert event.corruption is Corruption.UNIT_SWAP
    assert event.field == "crack_length_in"
    assert event.original == 2.0
    assert event.corrupted == pytest.approx(2.0 * 25.4)
    out = result.records[0]
    assert out.crack_length_in == pytest.approx(2.0 * 25.4)
    # Only crack_length_in changed on the targeted record.
    assert replace(out, crack_length_in=2.0) == records[0]
    changed, appended = _differing_indices(before, result.records)
    assert changed == [0] and appended == []
    assert records == before  # input untouched


def test_unit_swap_falls_back_to_total_time():
    records = [rec("A", total_time=1000.0)]
    result = inject(records, {Corruption.UNIT_SWAP: 1}, seed=1)
    (event,) = result.ledger
    assert event.field == "aircraft_total_time_hours"
    assert event.corrupted == pytest.approx(1000.0 * 60)
    assert result.records[0].aircraft_total_time_hours == pytest.approx(60000.0)


def test_part_number_swap_swaps_between_two_records():
    records = [
        rec("A", part_name="BRACKET", part_number="PN1"),
        rec("B", part_name="VALVE", part_number="PN2"),
    ]
    before = list(records)
    result = inject(records, {Corruption.PART_NUMBER_SWAP: 1}, seed=1)
    (event,) = result.ledger
    assert event.corruption is Corruption.PART_NUMBER_SWAP
    assert event.field == "part_number"
    assert set(event.control_numbers) == {"A", "B"}
    numbers = {r.control_number: r.part_number for r in result.records}
    assert numbers == {"A": "PN2", "B": "PN1"}
    assert event.original == ["PN1", "PN2"]
    assert event.corrupted == ["PN2", "PN1"]
    changed, appended = _differing_indices(before, result.records)
    assert changed == [0, 1] and appended == []
    assert records == before


def test_impossible_date_moves_difficulty_after_submission():
    sub = datetime(2026, 3, 1, 12, 0, tzinfo=EASTERN)
    records = [rec("A", difficulty=date(2026, 2, 1), submission=sub)]
    before = list(records)
    result = inject(records, {Corruption.IMPOSSIBLE_DATE: 1}, seed=1)
    (event,) = result.ledger
    assert event.field == "difficulty_date"
    assert event.original == "2026-02-01"
    out = result.records[0]
    assert out.difficulty_date > sub.date()
    assert event.corrupted == out.difficulty_date.isoformat()
    assert replace(out, difficulty_date=date(2026, 2, 1)) == records[0]
    changed, appended = _differing_indices(before, result.records)
    assert changed == [0] and appended == []
    assert records == before


def test_truncated_field_cuts_discrepancy_with_no_marker():
    text = "VALVE found LEAKING during SCHEDULED inspection on VG-450 airframe"
    records = [rec("A", discrepancy=text)]
    before = list(records)
    result = inject(records, {Corruption.TRUNCATED_FIELD: 1}, seed=1)
    (event,) = result.ledger
    assert event.field == "discrepancy"
    assert event.original == text
    out = result.records[0]
    assert out.discrepancy == event.corrupted
    assert text.startswith(out.discrepancy)
    assert 0 < len(out.discrepancy) < len(text)
    assert not out.discrepancy.endswith("...")
    assert not out.discrepancy.endswith("…")
    changed, appended = _differing_indices(before, result.records)
    assert changed == [0] and appended == []
    assert records == before


def test_conflicting_duplicate_adds_a_contradicting_copy():
    records = [rec("A", total_time=500.0)]
    before = list(records)
    result = inject(records, {Corruption.CONFLICTING_DUPLICATE: 1}, seed=1)
    (event,) = result.ledger
    assert event.field == "aircraft_total_time_hours"
    assert event.original == 500.0
    assert event.corrupted == pytest.approx(501.0)
    assert len(result.records) == len(records) + 1
    dup = result.records[event.output_index]
    assert dup.control_number == "A"
    assert dup.aircraft_total_time_hours == pytest.approx(501.0)
    changed, appended = _differing_indices(before, result.records)
    assert changed == [] and appended == [1]
    assert result.records[0] == records[0]  # original kept unchanged
    assert records == before


def test_conflicting_duplicate_uses_integer_cycles_field():
    records = [rec("A", total_time=None, total_cycles=5000)]
    result = inject(records, {Corruption.CONFLICTING_DUPLICATE: 1}, seed=1)
    (event,) = result.ledger
    assert event.field == "aircraft_total_cycles"
    assert event.original == 5000
    assert event.corrupted == 5001
    assert isinstance(event.corrupted, int)
    assert result.records[event.output_index].aircraft_total_cycles == 5001


def test_conflicting_duplicate_falls_back_to_crack_length():
    records = [rec("A", total_time=None, total_cycles=None, crack=3.0)]
    result = inject(records, {Corruption.CONFLICTING_DUPLICATE: 1}, seed=1)
    (event,) = result.ledger
    assert event.field == "crack_length_in"
    assert event.corrupted == pytest.approx(4.0)


def test_conflicting_duplicate_infeasible_without_numeric_field():
    records = [rec("A", total_time=None, total_cycles=None, crack=None)]
    with pytest.raises(InjectionError, match="CONFLICTING_DUPLICATE"):
        inject(records, {Corruption.CONFLICTING_DUPLICATE: 1}, seed=1)


def _realistic_records(tmp_path, n=40, seed=11):
    path = tmp_path / "s.csv"
    write_sdr_csv(make_sdr_rows(n, seed=seed), path)
    return load_sdr_csv(path).records


def test_determinism_same_inputs_identical_ledger(tmp_path):
    records = _realistic_records(tmp_path)
    before = list(records)
    plan = {
        Corruption.UNIT_SWAP: 2,
        Corruption.PART_NUMBER_SWAP: 1,
        Corruption.IMPOSSIBLE_DATE: 2,
        Corruption.TRUNCATED_FIELD: 2,
        Corruption.CONFLICTING_DUPLICATE: 1,
    }
    r1 = inject(records, plan, seed=99)
    r2 = inject(records, plan, seed=99)
    p1, p2 = tmp_path / "l1.jsonl", tmp_path / "l2.jsonl"
    write_ledger(r1.ledger, p1)
    write_ledger(r2.ledger, p2)
    assert p1.read_bytes() == p2.read_bytes()
    assert r1.records == r2.records
    assert records == before  # input untouched


def test_determinism_plan_order_does_not_matter(tmp_path):
    records = _realistic_records(tmp_path)
    plan_a = {Corruption.UNIT_SWAP: 1, Corruption.TRUNCATED_FIELD: 1}
    plan_b = {Corruption.TRUNCATED_FIELD: 1, Corruption.UNIT_SWAP: 1}
    r1 = inject(records, plan_a, seed=5)
    r2 = inject(records, plan_b, seed=5)
    assert [e.hash for e in r1.ledger] == [e.hash for e in r2.ledger]


def test_different_seed_gives_different_ledger(tmp_path):
    records = _realistic_records(tmp_path)
    plan = {Corruption.UNIT_SWAP: 2, Corruption.TRUNCATED_FIELD: 2}
    r1 = inject(records, plan, seed=1)
    r2 = inject(records, plan, seed=2)
    assert [e.hash for e in r1.ledger] != [e.hash for e in r2.ledger]


def test_no_record_gets_two_corruptions(tmp_path):
    records = _realistic_records(tmp_path, n=10, seed=3)
    plan = {Corruption.UNIT_SWAP: 2, Corruption.CONFLICTING_DUPLICATE: 2}
    result = inject(records, plan, seed=4)
    touched = [i for e in result.ledger for i in [e.output_index] if i < len(records)]
    # Source indices used by in-place corruptions must be distinct.
    inplace = [e.output_index for e in result.ledger if e.corruption is Corruption.UNIT_SWAP]
    assert len(inplace) == len(set(inplace))
    assert len(touched) == len(set(touched))


def test_verify_ledger_true_then_false_on_tampering(tmp_path):
    records = _realistic_records(tmp_path)
    plan = {Corruption.UNIT_SWAP: 2, Corruption.TRUNCATED_FIELD: 2, Corruption.IMPOSSIBLE_DATE: 1}
    ledger = inject(records, plan, seed=7).ledger
    assert len(ledger) >= 3
    assert verify_ledger(ledger) is True

    edited = list(ledger)
    edited[1] = replace(edited[1], corrupted="tampered")
    assert verify_ledger(edited) is False

    deleted = ledger[:1] + ledger[2:]  # drop an interior event
    assert verify_ledger(deleted) is False

    swapped = list(ledger)
    swapped[0], swapped[1] = swapped[1], swapped[0]
    assert verify_ledger(swapped) is False


def test_infeasible_plan_raises():
    records = [rec("A", crack=1.0)]
    with pytest.raises(InjectionError, match="UNIT_SWAP"):
        inject(records, {Corruption.UNIT_SWAP: 2}, seed=1)


def test_part_number_swap_needs_two_different_part_names():
    records = [
        rec("A", part_name="BRACKET", part_number="PN1"),
        rec("B", part_name="BRACKET", part_number="PN2"),
    ]
    with pytest.raises(InjectionError, match="part names"):
        inject(records, {Corruption.PART_NUMBER_SWAP: 1}, seed=1)


def test_part_number_swap_needs_two_records():
    records = [rec("A", part_name="BRACKET")]
    with pytest.raises(InjectionError, match="two unused records"):
        inject(records, {Corruption.PART_NUMBER_SWAP: 1}, seed=1)
