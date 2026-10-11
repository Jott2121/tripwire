import re
from datetime import datetime, timedelta

import pytest

from tripwire.sdr import SDR_COLUMNS
from tripwire.synth import make_sdr_rows, write_sdr_csv


def test_submission_date_has_millisecond_precision_and_the_submitter_designator_prefix():
    for row in make_sdr_rows(20, seed=3):
        stamp = row["SubmissionDate"]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}-05:00", stamp), stamp
        assert row["SubmitterDesignator"] == row["OperatorControlNumber"][:4] == "TWSY"


def test_rows_have_the_full_documented_header():
    # The SDR-<year>.csv download has 76 columns; 77 is the batch upload
    # layout. The loader maps by column name and reads the live file's own
    # header, so a real file with an extra column still loads (docs/DATA.md).
    rows = make_sdr_rows(4, seed=1)
    assert len(SDR_COLUMNS) == 76
    assert len(set(SDR_COLUMNS)) == 76  # no duplicate names
    for row in rows:
        assert set(row) == set(SDR_COLUMNS)


def test_same_seed_is_byte_identical(tmp_path):
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    write_sdr_csv(make_sdr_rows(25, seed=42), a)
    write_sdr_csv(make_sdr_rows(25, seed=42), b)
    assert a.read_bytes() == b.read_bytes()


def test_different_seed_differs(tmp_path):
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    write_sdr_csv(make_sdr_rows(25, seed=42), a)
    write_sdr_csv(make_sdr_rows(25, seed=43), b)
    assert a.read_bytes() != b.read_bytes()


def test_negative_n_raises():
    with pytest.raises(ValueError, match="non-negative"):
        make_sdr_rows(-1, seed=1)


# Code values observed in a profile of the real SDR-2026.csv (docs/DATA.md).
_OBSERVED_SUBMITTER_TYPES = set("ABCDEGHIMPSWZ")
_CONTROL_NUMBER = re.compile(r"^TWSY(\d{8})(\d{4})$")
_REGISTRY_MARK = re.compile(r"^[1-9]\d{0,2}[A-HJ-NP-Z]{0,2}$")


def test_rows_follow_observed_sdr_conventions():
    rows = make_sdr_rows(200, seed=3)
    for i, row in enumerate(rows):
        assert row["SDRType"] in {"A", "G"}
        assert row["SubmitterTypeCode"] in _OBSERVED_SUBMITTER_TYPES
        assert re.fullmatch(r"[A-Z]{2}", row["StageOfOperationCode"])

        submitted = datetime.fromisoformat(row["SubmissionDate"])
        assert submitted.utcoffset() == timedelta(hours=-5)
        assert row["SubmissionDate"].endswith("-05:00")
        match = _CONTROL_NUMBER.fullmatch(row["OperatorControlNumber"])
        assert match, row["OperatorControlNumber"]
        assert match.group(1) == submitted.strftime("%Y%m%d")
        assert int(match.group(2)) == i + 1
        assert len(row["OperatorControlNumber"]) <= 17

        assert "." not in row["AircraftTotalTime"]
        assert row["AircraftTotalTime"].isdigit()
        assert "." not in row["AircraftTotalCycles"]
        assert row["AircraftTotalCycles"].isdigit()

        assert _REGISTRY_MARK.fullmatch(row["RegistryNNumber"]), row["RegistryNNumber"]
        assert not row["RegistryNNumber"].startswith("N")


def test_registry_marks_cover_digit_and_letter_suffix_forms():
    marks = [row["RegistryNNumber"] for row in make_sdr_rows(200, seed=3)]
    suffix_lengths = {len(m) - len(m.rstrip("ABCDEFGHJKLMNPQRSTUVWXYZ")) for m in marks}
    assert suffix_lengths == {0, 1, 2}


def test_control_numbers_are_unique():
    rows = make_sdr_rows(500, seed=8)
    numbers = [row["OperatorControlNumber"] for row in rows]
    assert len(set(numbers)) == len(numbers)


def test_n_above_four_digit_sequence_raises():
    with pytest.raises(ValueError, match="at most 9999"):
        make_sdr_rows(10000, seed=1)
