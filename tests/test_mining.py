"""Mining rules mine-v0 over synthetic SDRs and a synthetic registry. No network, no model."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from tripwire import mining as m
from tripwire.registry import AircraftReference, Registry, RegistryRecord
from tripwire.sdr import SDRRecord

REG = Registry.build(
    [
        RegistryRecord("101", "ASN-1001", "0010101", 2005, "4", "V", date(2020, 1, 1)),
        RegistryRecord("202", "ASN2002", "0020202", 2019, "4", "V", date(2026, 6, 1)),
        RegistryRecord("303", "ASN3003", "0010101", None, "4", "V", None),
        RegistryRecord("404", "A1", "0010101", 2001, "4", "V", None),
        RegistryRecord("404", "A2", "0010101", 2001, "4", "V", None),
        RegistryRecord("505", "ASN5005", "0020202", 2027, "4", "V", None),
    ],
    [
        AircraftReference("0010101", "ACME AERO WORKS", "AW-100", "4", 1, 4),
        AircraftReference("0020202", "ZENITH AIRCRAFT CORP", "ZX-9", "5", 2, 12),
    ],
)
BASE = SDRRecord(
    control_number="TWSY202601020001", difficulty_date=date(2026, 1, 2),
    submission_date=datetime(2026, 1, 2, tzinfo=UTC), jasc_code="3200", aircraft_make="ACME",
    aircraft_model="AW-100", aircraft_total_time_hours=1000.0, aircraft_total_cycles=900,
    part_name="BRACKET", part_number="P-1", part_condition="CRACKED", crack_length_in=0.2,
    discrepancy="FICTIONAL TEXT", registry_n_number="101", aircraft_serial_number="asn 1001",
)  # fmt: skip


def one(**over):
    return m.mine_one(replace(BASE, **over), REG)


def test_a_consistent_row_gives_controls_only():
    found, controls, skip = one()
    assert found == [] and skip is None
    assert {(c.rule, c.field, c.expected) for c in controls} == {
        ("control", "aircraft_serial_number", "agree"),
        ("control", "aircraft_total_time", "agree"),
    }


def test_serial_mismatch_settles_and_blocks_the_time_rules():
    found, controls, _ = one(aircraft_serial_number="ASN9999", aircraft_total_time_hours=9e9)
    assert [(i.rule, i.sdr_value, i.registry_value) for i in found] == [
        ("S1", "ASN9999", "ASN-1001")
    ]  # T1 is not applied to what may be another airframe
    assert controls == []


def test_total_time_beyond_the_calendar_is_settled():
    limit = m.calendar_hours(2005, date(2026, 1, 2))
    assert limit == ((date(2026, 1, 2) - date(2005, 1, 1)).days + 1) * 24
    assert one(aircraft_total_time_hours=limit)[0] == []  # exactly possible
    found, _, _ = one(aircraft_total_time_hours=limit + 1)
    assert [i.rule for i in found] == ["T1"] and found[0].expected == "disagree"


def test_report_before_the_year_built_is_settled():
    found, _, _ = one(registry_n_number="505", aircraft_serial_number="ASN5005",
                      aircraft_make="ZENITH")  # fmt: skip
    assert [(i.rule, i.sdr_value, i.registry_value) for i in found] == [
        ("D1", "2026-01-02", "2027")
    ]


def test_make_mismatch_is_unsettled_and_generic_words_do_not_match():
    found, _, _ = one(aircraft_make="ZENITH")
    assert [i.rule for i in found] == ["M1"]
    assert m.words("Acme Aircraft Inc") == {"ACME"}
    assert one(aircraft_make="ACME AERO")[0] == []
    assert one(aircraft_make="AIRCRAFT CORP")[0][0].rule == "M1"  # only generic words: no match


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"registry_n_number": ""}, "no_n_number"),
        ({"registry_n_number": "999"}, "not_in_registry"),
        ({"registry_n_number": "N404"}, "duplicate_n_number"),
        ({"registry_n_number": "202"}, "registered_after_report"),
    ],
)
def test_rows_a_rule_cannot_settle_are_skipped(over, reason):
    found, controls, skip = one(**over)
    assert found == controls == [] and skip == m.Skip(BASE.control_number, reason)


def test_missing_values_skip_a_rule_not_the_row():
    found, controls, skip = one(registry_n_number="303", aircraft_serial_number="ASN3003")
    assert (
        skip is None and found == [] and [c.field for c in controls] == ["aircraft_serial_number"]
    )  # no year built: no time rule
    assert one(aircraft_total_time_hours=None, aircraft_serial_number="")[1] == []


def test_mine_splits_settled_unsettled_controls_and_counts():
    rows = [
        replace(BASE),
        replace(BASE, control_number="A2", aircraft_serial_number="X"),
        replace(BASE, control_number="A3", aircraft_make="ZENITH"),
        replace(BASE, control_number="A4", registry_n_number=""),
    ]
    out = m.mine(rows, REG)
    assert [i.rule for i in out.settled] == ["S1"] and [i.rule for i in out.unsettled] == ["M1"]
    assert len(out.controls) == 2
    c = out.counts()
    assert c["S1"] == 1 and c["M1"] == 1 and c["T1"] == 0 and c["skip:no_n_number"] == 1


def test_sample_is_deterministic_and_salted():
    items = m.mine([replace(BASE, control_number=f"C{i}") for i in range(20)], REG).controls
    a, b = m.sample(items, 5, "s"), m.sample(list(reversed(items)), 5, "s")
    assert a == b and len(a) == 5 and m.sample(items, 5, "other") != a


def test_fingerprint_is_the_file_hash():
    import hashlib
    from pathlib import Path

    assert m.fingerprint() == hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()
    assert set(m.SETTLED) < set(m.RULES) and m.RULES_VERSION == "mine-v0"
