"""tripwire.contract: quantities, provenance, records, fail-closed judging, fingerprints."""

from datetime import date, datetime, timedelta, timezone

import pytest

from tripwire import contract as c
from tripwire import standards as s

TZ = timezone(timedelta(hours=-5))
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=TZ)
PROV = c.Provenance("synthetic-lots", "https://github.com/Jott2121/tripwire", "seed-1", NOW)


def _defect(**over):
    kw = dict(
        defect_id="D1",
        difficulty_date=date(2026, 1, 2),
        submission_date=datetime(2026, 1, 5, 9, 0, tzinfo=TZ),
        jasc_code="2820",
        registry="12345",
        part_number="PN1",
        part_condition="CRACKED",
        discrepancy="bracket cracked",
        provenance=PROV,
        crack_length=c.Quantity(1.25, s.Unit.INCH),
    )
    kw.update(over)
    return c.Defect(**kw)


def _spec(**over):
    kw = dict(
        spec_id="SPEC-A",
        revision="r2",
        parameter="pull_min",
        applies_to="AWG 22",
        source_url="https://example.invalid/spec",
        provenance=PROV,
        minimum=c.Quantity(57.0, s.Unit.NEWTON),
    )
    kw.update(over)
    return c.SpecLimit(**kw)


def _test(**over):
    kw = dict(
        test_id="T1",
        lot_id="L1",
        parameter="pull_min",
        measured=c.Quantity(15.0, s.Unit.POUND_FORCE),
        spec_id="SPEC-A",
        applies_to="AWG 22",
        spec_revision="r2",
        tested_at=NOW,
        provenance=PROV,
    )
    kw.update(over)
    return c.TestResult(**kw)


def test_quantity_converts_through_standards_and_fails_closed_across_dimensions():
    q = c.Quantity(2.0, s.Unit.INCH)
    assert q.to(s.Unit.MILLIMETRE) == c.Quantity(50.8, s.Unit.MILLIMETRE)
    assert q.dimension is s.Dimension.LENGTH
    with pytest.raises(c.ContractError):
        q.to(s.Unit.HOUR)


@pytest.mark.parametrize(
    "value, unit",
    [
        (float("nan"), s.Unit.INCH),
        (float("inf"), s.Unit.INCH),
        ("1.0", s.Unit.INCH),
        (True, s.Unit.INCH),
        (None, s.Unit.INCH),
        (1.0, "in"),
    ],
)
def test_quantity_rejects_non_finite_non_numeric_or_bare_string_unit(value, unit):
    with pytest.raises(c.ContractError):
        c.Quantity(value, unit)


@pytest.mark.parametrize(
    "override",
    [
        {"source": " "},
        {"source_url": ""},
        {"revision": " "},
        {"retrieved_at": datetime(2026, 1, 1)},
    ],
)
def test_provenance_requires_every_field_and_a_tz_aware_time(override):
    with pytest.raises(c.ContractError):
        c.Provenance(**{**vars(PROV), **override})


def test_defect_validates_identifiers_dates_and_dimensions():
    assert _defect().crack_length.unit is s.Unit.INCH
    assert _defect(registry="").registry == ""
    with pytest.raises(c.ContractError):
        _defect(jasc_code="28A0")
    with pytest.raises(c.ContractError):
        _defect(registry="N12345")  # SDR convention omits the N
    with pytest.raises(c.ContractError):
        _defect(difficulty_date=date(2026, 1, 6))
    with pytest.raises(c.ContractError):
        _defect(submission_date=datetime(2026, 1, 5))
    with pytest.raises(c.ContractError):
        _defect(crack_length=c.Quantity(1.0, s.Unit.HOUR))
    with pytest.raises(c.ContractError):
        _defect(aircraft_total_time=c.Quantity(1.0, s.Unit.CYCLE))
    with pytest.raises(c.ContractError):
        _defect(part_number="")


def test_spec_limit_needs_a_bound_and_consistent_bounds():
    both = _spec(maximum=c.Quantity(20.0, s.Unit.POUND_FORCE))
    assert both.unit is s.Unit.NEWTON and both.is_current
    with pytest.raises(c.ContractError):
        _spec(minimum=None)
    with pytest.raises(c.ContractError):
        _spec(maximum=c.Quantity(10.0, s.Unit.NEWTON))  # max below min
    with pytest.raises(c.ContractError):
        _spec(maximum=c.Quantity(10.0, s.Unit.INCH))  # mixed dimensions
    with pytest.raises(c.ContractError):
        _spec(applies_to="")


def test_spec_check_compares_in_the_limits_unit():
    spec = _spec(maximum=c.Quantity(100.0, s.Unit.NEWTON))
    assert spec.check(c.Quantity(15.0, s.Unit.POUND_FORCE))  # 66.7 N
    assert not spec.check(c.Quantity(12.0, s.Unit.POUND_FORCE))  # 53.4 N
    assert not spec.check(c.Quantity(101.0, s.Unit.NEWTON))
    assert _spec(minimum=None, maximum=c.Quantity(1.0, s.Unit.INCH)).check(
        c.Quantity(25.0, s.Unit.MILLIMETRE)
    )
    with pytest.raises(c.ContractError):
        spec.check(c.Quantity(1.0, s.Unit.INCH))


def test_test_result_judges_only_against_the_matching_current_revision():
    assert _test().judge(_spec()) is True
    assert _test(measured=c.Quantity(10.0, s.Unit.POUND_FORCE)).judge(_spec()) is False
    with pytest.raises(c.ContractError):
        _test().judge(_spec(revision="r3"))  # stale-revision mismatch (F2)
    with pytest.raises(c.ContractError):
        _test().judge(_spec(superseded_by="r3"))
    with pytest.raises(c.ContractError):
        _test().judge(_spec(parameter="other"))
    with pytest.raises(c.ContractError):
        _test().judge(_spec(applies_to="AWG 20"))  # a different row of the same spec
    with pytest.raises(c.ContractError):
        _test(applies_to=" ")
    with pytest.raises(c.ContractError):
        _test(tested_at=datetime(2026, 1, 1))


def test_lot_and_work_order_validation():
    lot = c.Lot("L1", "PN1", 10, date(2026, 1, 1), "ACME AERO", PROV)
    assert lot.quantity == 10
    for bad in (0, -1, True, 1.5):
        with pytest.raises(c.ContractError):
            c.Lot("L1", "PN1", bad, date(2026, 1, 1), "ACME AERO", PROV)
    wo = c.WorkOrder("WO1", "D1", "replace bracket", NOW, "agent", PROV)
    assert wo.status is c.WorkOrderStatus.OPEN
    with pytest.raises(c.ContractError):
        c.WorkOrder("WO1", "D1", "x", NOW, "agent", PROV, status="open")
    with pytest.raises(c.ContractError):
        c.WorkOrder("WO1", "D1", "x", datetime(2026, 1, 1), "agent", PROV)
    with pytest.raises(c.ContractError):
        c.WorkOrder("", "D1", "x", NOW, "agent", PROV)


def test_canonical_json_is_sorted_compact_and_fingerprints_are_stable():
    wo = c.WorkOrder("WO1", "D1", "replace bracket", NOW, "agent", PROV)
    text = c.canonical_json(wo)
    assert text.startswith('{"action":"replace bracket","created_at":"2026-10-11T12:00:00-05:00"')
    assert '"status":"open"' in text and " " not in text.replace("replace bracket", "")
    assert c.fingerprint(wo) == c.fingerprint(c.WorkOrder(**vars(wo)))
    assert c.fingerprint(wo) != c.fingerprint(
        c.WorkOrder(**{**vars(wo), "status": c.WorkOrderStatus.CLOSED})
    )
    assert c.to_jsonable(_defect())["crack_length"] == {"unit": "in", "value": 1.25}
    assert c.to_jsonable([(1, None)]) == [[1, None]]
    with pytest.raises(c.ContractError):
        c.to_jsonable(object())
