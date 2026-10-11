from datetime import date, datetime

import pytest

from tripwire.sdr import (
    SDR_COLUMNS,
    LoadResult,
    SDRFormatError,
    SDRRecord,
    load_sdr_csv,
)
from tripwire.synth import make_sdr_rows, write_sdr_csv


def _write(tmp_path, rows, name="sdr.csv"):
    path = tmp_path / name
    write_sdr_csv(rows, path)
    return path


def test_loads_all_synthetic_records(tmp_path):
    rows = make_sdr_rows(12, seed=1)
    result = load_sdr_csv(_write(tmp_path, rows))
    assert isinstance(result, LoadResult)
    assert len(result.records) == 12
    assert result.rejects == []


def test_every_field_has_the_right_type(tmp_path):
    rows = make_sdr_rows(20, seed=7)
    result = load_sdr_csv(_write(tmp_path, rows))
    for rec in result.records:
        assert isinstance(rec, SDRRecord)
        assert isinstance(rec.control_number, str)
        assert isinstance(rec.difficulty_date, date)
        assert isinstance(rec.submission_date, datetime)
        assert isinstance(rec.jasc_code, str) and len(rec.jasc_code) == 4
        assert isinstance(rec.aircraft_make, str)
        assert isinstance(rec.aircraft_model, str)
        assert rec.aircraft_total_time_hours is None or isinstance(
            rec.aircraft_total_time_hours, float
        )
        assert rec.aircraft_total_cycles is None or isinstance(rec.aircraft_total_cycles, int)
        assert isinstance(rec.part_name, str)
        assert isinstance(rec.part_number, str)
        assert isinstance(rec.part_condition, str)
        assert rec.crack_length_in is None or isinstance(rec.crack_length_in, float)
        assert isinstance(rec.discrepancy, str)


def test_empty_numeric_fields_map_to_none(tmp_path):
    rows = make_sdr_rows(1, seed=3)
    rows[0]["CrackLength"] = ""
    rows[0]["AircraftTotalTime"] = ""
    rows[0]["AircraftTotalCycles"] = ""
    result = load_sdr_csv(_write(tmp_path, rows))
    rec = result.records[0]
    assert rec.crack_length_in is None
    assert rec.aircraft_total_time_hours is None
    assert rec.aircraft_total_cycles is None


def test_submission_date_is_timezone_aware(tmp_path):
    rows = make_sdr_rows(3, seed=5)
    result = load_sdr_csv(_write(tmp_path, rows))
    for rec in result.records:
        assert rec.submission_date.tzinfo is not None
        assert rec.submission_date.utcoffset() is not None


def test_malformed_date_row_is_rejected_with_line_number(tmp_path):
    rows = make_sdr_rows(5, seed=9)
    rows[2]["DifficultyDate"] = "not-a-date"
    result = load_sdr_csv(_write(tmp_path, rows))
    assert len(result.records) == 4
    assert len(result.rejects) == 1
    # header is physical line 1; data row index 2 is physical line 4.
    assert result.rejects[0].line == 4
    assert "not-a-date" in result.rejects[0].reason


def test_wrong_column_count_row_is_rejected(tmp_path):
    rows = make_sdr_rows(2, seed=2)
    path = _write(tmp_path, rows)
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write("too,few,columns\r\n")
    result = load_sdr_csv(path)
    assert len(result.records) == 2
    assert len(result.rejects) == 1
    assert result.rejects[0].line == 4


def test_missing_required_column_raises(tmp_path):
    bad_header = [c for c in SDR_COLUMNS if c != "JASCCode"]
    path = tmp_path / "bad.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(",".join(bad_header) + "\r\n")
    with pytest.raises(SDRFormatError, match="JASCCode"):
        load_sdr_csv(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(SDRFormatError, match="not found"):
        load_sdr_csv(tmp_path / "nope.csv")


def test_empty_file_raises(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(SDRFormatError, match="empty"):
        load_sdr_csv(path)


def test_non_four_digit_jasc_is_rejected(tmp_path):
    rows = make_sdr_rows(3, seed=8)
    rows[1]["JASCCode"] = "ABC"
    result = load_sdr_csv(_write(tmp_path, rows))
    assert len(result.records) == 2
    assert len(result.rejects) == 1
    assert result.rejects[0].line == 3
    assert "JASCCode" in result.rejects[0].reason


def test_naive_submission_date_is_rejected(tmp_path):
    rows = make_sdr_rows(2, seed=10)
    rows[0]["SubmissionDate"] = "2026-01-02T14:09:41"  # no UTC offset
    result = load_sdr_csv(_write(tmp_path, rows))
    assert len(result.records) == 1
    assert len(result.rejects) == 1
    assert "timezone-aware" in result.rejects[0].reason


def test_utf8_text_round_trips(tmp_path):
    rows = make_sdr_rows(1, seed=6)
    rows[0]["Discrepancy"] = "corrosión found during inspection café"
    path = _write(tmp_path, rows)
    assert "café".encode() in path.read_bytes()  # written as UTF-8, not latin-1
    result = load_sdr_csv(path)
    assert result.records[0].discrepancy == "corrosión found during inspection café"


def test_invalid_utf8_byte_raises_format_error_with_offset(tmp_path):
    rows = make_sdr_rows(3, seed=4)
    rows[1]["Discrepancy"] = "BADBYTEHERE"
    path = _write(tmp_path, rows)
    clean = path.read_bytes()
    offset = clean.index(b"BADBYTEHERE") + 3
    # A lone 0xE9 is latin-1 "é" but an invalid UTF-8 start byte here.
    path.write_bytes(clean[:offset] + b"\xe9" + clean[offset:])
    with pytest.raises(SDRFormatError, match=rf"byte offset {offset} \(line 3\)") as info:
        load_sdr_csv(path)
    assert isinstance(info.value.__cause__, UnicodeDecodeError)
