"""tripwire.standards: units, conversions, identifier rules, code lists, doc sync."""

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tripwire import standards as s

DOC = Path(__file__).resolve().parents[1] / "docs" / "MEASUREMENT_STANDARDS.md"


# --- units and conversions --------------------------------------------------


def test_every_unit_has_a_dimension_and_every_dimension_a_canonical_unit():
    assert set(s.UNIT_DIMENSION) == set(s.Unit)
    assert set(s.CANONICAL_UNIT) == set(s.Dimension)
    for dim, unit in s.CANONICAL_UNIT.items():
        assert s.UNIT_DIMENSION[unit] is dim


def test_conversion_factors_are_the_defined_exact_values():
    assert s.convert(1.0, s.Unit.INCH, s.Unit.MILLIMETRE) == 25.4
    assert s.convert(25.4, s.Unit.MILLIMETRE, s.Unit.INCH) == 1.0
    assert s.convert(2.0, s.Unit.HOUR, s.Unit.MINUTE) == 120.0
    assert s.convert(1.0, s.Unit.POUND_FORCE, s.Unit.NEWTON) == 4.4482216152605
    assert s.convert(3.5, s.Unit.CYCLE, s.Unit.CYCLE) == 3.5


def test_conversion_across_dimensions_fails_closed():
    with pytest.raises(s.StandardsError):
        s.convert(1.0, s.Unit.INCH, s.Unit.HOUR)
    with pytest.raises(s.StandardsError):
        s.convert(1.0, s.Unit.HOUR, s.Unit.CYCLE)


def test_conversion_without_a_defined_factor_fails_closed(monkeypatch):
    monkeypatch.setattr(s, "CONVERSION_FACTORS", {})
    with pytest.raises(s.StandardsError):
        s.convert(1.0, s.Unit.INCH, s.Unit.MILLIMETRE)


# --- the SDR field registry --------------------------------------------------


def test_sdr_fields_are_the_76_named_header_columns_in_order_and_unique():
    assert len(s.SDR_FIELDS) == 76
    assert len(set(s.SDR_COLUMNS)) == 76
    assert s.SDR_COLUMNS[0] == "OperatorControlNumber"
    assert s.SDR_COLUMNS[-1] == "Discrepancy"
    assert s.SDR_FIELD_BY_NAME["CrackLength"].unit is s.Unit.INCH
    assert s.SDR_FIELD_BY_NAME["AircraftTotalTime"].unit is s.Unit.HOUR
    assert s.SDR_FIELD_BY_NAME["AircraftTotalCycles"].unit is s.Unit.CYCLE


def test_every_field_cites_a_source_and_units_match_kinds():
    for f in s.SDR_FIELDS:
        assert f.source.startswith("https://"), f.column
        assert f.note, f.column
        if f.kind in (s.FieldKind.NUMERIC, s.FieldKind.INTEGER):
            assert f.unit is not s.Unit.NONE, f.column
        else:
            assert f.unit is s.Unit.NONE, f.column
        if f.pattern is not None:
            re.compile(f.pattern)


def test_no_field_is_unresolved_after_the_profile_and_open_items_remain_honest():
    assert not [f.column for f in s.SDR_FIELDS if f.confidence is s.Confidence.UNRESOLVED]
    assert "code_tables" in s.UNRESOLVED_KEYS  # meanings of observed values are still unknown
    assert all(not cl.complete for cl in s.CODE_LISTS)


def test_unresolved_keys_are_unique_and_actionable():
    assert len(s.UNRESOLVED_KEYS) == len(s.UNRESOLVED)
    assert not (s.UNRESOLVED_KEYS & s.RESOLVED_KEYS)
    assert len(s.RESOLVED_KEYS) == len(s.RESOLVED) == 7
    assert s.SDR_ENCODING == "utf-8"
    assert s.SDR_FIELD_BY_NAME["SubmissionDate"].confidence is s.Confidence.HIGH
    for u in s.UNRESOLVED:
        assert u.question.endswith("?") or u.question.endswith(".")
        assert u.how_to_resolve


# --- code lists ----------------------------------------------------------------


def test_code_list_check_is_tri_state():
    assert s.SUBMITTER_TYPE.check("A") is True
    assert s.SUBMITTER_TYPE.check("Q") is None  # partial list: unknown, not rejected
    assert set("ABCDEGHIMPSWZ") <= set(s.SUBMITTER_TYPE.codes)  # all observed in SDR-2026
    assert set(s.SDR_TYPE.codes) == {"A", "G"} and s.SDR_TYPE.check("S") is None
    assert {"IN", "TX", "NR", "TO", "LD", "UK"} <= set(s.STAGE_OF_OPERATION.codes)
    assert set(s.HOW_DISCOVERED.codes) == set("VOCE9UBMXLTDW")
    assert set(s.SINCE_CODE.codes) == {"O", "R", "I"} and set(s.SIDE_CODE.codes) == {"L", "R"}
    assert len(s.RECEIVING_REGION.codes) == 10 and s.RECEIVING_REGION.check("SW") is True
    assert set("ABCDEFGHIJKLMNOPRSTWXYZ") <= set(s.NATURE_OF_CONDITION.codes)
    complete = s.CodeList("x", {"1": "one"}, True, s.Confidence.HIGH, "https://example.invalid")
    assert complete.check("1") is True
    assert complete.check("2") is False


def test_code_lists_are_honest_about_completeness():
    for cl in s.CODE_LISTS:
        assert cl.source.startswith("https://")
        assert cl.codes
        assert cl.complete is False  # nothing is marked complete until the full table is read
        assert cl.confidence in (s.Confidence.MEDIUM, s.Confidence.HIGH)


def test_jasc_titles_cover_the_synthetic_generator_codes():
    assert set(s.JASC_TITLES.codes) == {"2820", "3220", "3230", "5320", "5710", "7230"}


# --- identifiers ---------------------------------------------------------------


@pytest.mark.parametrize(
    "mark",
    ["N1", "N12345", "N1234A", "N123AB", "N1A", "N9ZZ", "N10", "N100AB"],
)
def test_valid_n_numbers(mark):
    assert s.parse_n_number(mark) == mark
    assert s.is_valid_n_number(mark)
    assert s.N_NUMBER_RE.fullmatch(mark)


@pytest.mark.parametrize(
    "mark",
    ["N", "N0", "N0123", "N123456", "N12ABC", "N12I", "NO123", "NABC", "N1234AB", "", "N-123"],
)
def test_invalid_n_numbers(mark):
    assert not s.is_valid_n_number(mark)
    with pytest.raises(s.StandardsError):
        s.parse_n_number(mark)


def test_sdr_registry_convention_omits_the_prefix():
    assert s.parse_n_number("12345", prefix=False) == "N12345"
    assert s.parse_n_number("123ab", prefix=None) == "N123AB"
    with pytest.raises(s.StandardsError):
        s.parse_n_number("N12345", prefix=False)
    with pytest.raises(s.StandardsError):
        s.parse_n_number("12345", prefix=True)


def test_jasc_system_is_the_first_two_digits():
    assert s.jasc_system("2820") == "28"
    for bad in ("282", "28200", "28A0", ""):
        with pytest.raises(s.StandardsError):
            s.jasc_system(bad)


def test_control_number_parses_designator_date_sequence():
    cn = s.parse_control_number("AAIA202110010001")
    assert cn == s.ControlNumber("AAIA", date(2021, 10, 1), "0001")
    assert cn.text == "AAIA202110010001"
    assert s.parse_control_number("abcd2026010199999").sequence == "99999"


@pytest.mark.parametrize(
    "bad",
    ["", "OCN000001", "AAIA20211001000001", "AAIA20211301", "AAI2021100100", "AAIA2021100-0"],
)
def test_control_number_rejects_wrong_shape_or_impossible_date(bad):
    with pytest.raises(s.StandardsError):
        s.parse_control_number(bad)


# --- dates -----------------------------------------------------------------------


def test_difficulty_date_is_mm_dd_yyyy():
    assert s.parse_difficulty_date("01/02/2026") == date(2026, 1, 2)
    for bad in ("2026-01-02", "13/01/2026", "1/2/26x", ""):
        with pytest.raises(s.StandardsError):
            s.parse_difficulty_date(bad)


def test_submission_datetime_requires_an_offset():
    parsed = s.parse_submission_datetime("2026-01-02T14:09:41.140-05:00")
    assert parsed.utcoffset() == timedelta(hours=-5)
    for bad in ("2026-01-02T14:09:41.140", "not a date", ""):
        with pytest.raises(s.StandardsError):
            s.parse_submission_datetime(bad)


def test_difficulty_precedes_submission():
    sub = datetime(2026, 1, 5, 9, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert s.difficulty_precedes_submission(date(2026, 1, 2), sub)
    assert s.difficulty_precedes_submission(date(2026, 1, 5), sub)
    assert not s.difficulty_precedes_submission(date(2026, 1, 6), sub)
    assert s.PART_121_REPORT_DEADLINE_HOURS == 96


# --- the doc and the module stay in sync -------------------------------------------


def test_doc_mentions_every_column_source_and_open_item():
    text = DOC.read_text(encoding="utf-8")
    for f in s.SDR_FIELDS:
        assert f"`{f.column}`" in text, f.column
    for url in {f.source for f in s.SDR_FIELDS} | {cl.source for cl in s.CODE_LISTS}:
        assert url in text, url
    for u in s.UNRESOLVED:
        assert f"`{u.key}`" in text, u.key
    for r in s.RESOLVED:
        assert f"`{r.key}`" in text, r.key
    assert "reports/sdr_profile_2026.json" in text
    assert (DOC.parent.parent / "reports" / "sdr_profile_2026.json").exists()
    for level in s.Confidence:
        assert level.value in text
