"""scripts/profile_sdr.py: offline profiler for a downloaded SDR CSV (synthetic rows only)."""

import csv
import importlib.util
import json
import pathlib

import pytest

from tripwire import standards as s

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("profile_sdr", ROOT / "scripts" / "profile_sdr.py")
prof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prof)

SENTINEL = "ZORBLAX NARRATIVE SENTINEL"


def _row(**overrides: str) -> dict[str, str]:
    row = dict.fromkeys(s.SDR_COLUMNS, "")
    row.update(
        OperatorControlNumber="AAIA202601020001",
        DifficultyDate="01/02/2026",
        SubmissionDate="2026-01-05T14:09:41.140-05:00",
        OperatorDesignator="AAIA",
        SubmitterTypeCode="A",
        JASCCode="2820",
        StageOfOperationCode="CR",
        RegistryNNumber="12345",
        AircraftTotalTime="12345",
        AircraftTotalCycles="678",
        CrackLength="1.25",
        Discrepancy=SENTINEL,
    )
    row.update(overrides)
    return row


def _write(path, rows, header=s.SDR_COLUMNS, encoding="utf-8"):
    with path.open("w", encoding=encoding, newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, lineterminator="\r\n", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in header})
    return path


@pytest.fixture
def sample(tmp_path):
    rows = [
        _row(),
        _row(
            OperatorControlNumber="OCN000002",
            SubmissionDate="2026-07-01T09:00:00-04:00",
            DifficultyDate="07/03/2026",
            RegistryNNumber="N678AB",
            AircraftTotalTime="100.5",
            Discrepancy="café " + SENTINEL,
        ),
        _row(RegistryNNumber="", AircraftTotalTime="", CrackLength=""),
    ]
    return _write(tmp_path / "SDR-2026.csv", rows)


def test_answers_cover_every_profile_backed_open_item(sample):
    report = prof.profile(sample)
    answers = report["answers"]
    for key in ("column_count", "encoding", "submission_tz", "whole_hours", "registry_prefix"):
        assert key in answers and key in s.UNRESOLVED_KEYS
    assert "control_number_shape" in answers
    assert answers["column_count"] == {
        "header_columns": 76,
        "expected_named_columns": 76,
        "missing_from_file": [],
        "extra_in_file": [],
        "order_matches_expected": True,
    }
    assert answers["encoding"] == {"latin-1": 0, "utf-8": 0, "cp1252": 0}
    assert answers["submission_tz"] == {"2026-01": {"-05:00": 2}, "2026-07": {"-04:00": 1}}
    assert answers["whole_hours"]["AircraftTotalTime"] == 0.5
    assert answers["registry_prefix"] == {
        "filled": 2,
        "starts_with_n": 1,
        "valid_without_prefix": 1,
        "valid_with_prefix": 1,
    }
    assert answers["control_number_shape"]["matches_documented_shape"] == 2
    assert answers["control_number_shape"]["length_histogram"] == {9: 1, 16: 2}
    lag = answers["difficulty_to_submission_lag_days"]
    assert lag["n"] == 3 and lag["negative"] == 1 and lag["max"] == 3


def test_column_stats_report_shapes_units_and_code_values(sample):
    cols = prof.profile(sample)["columns"]
    assert cols["CrackLength"]["expected_unit"] == "in"
    assert cols["CrackLength"]["numeric"]["integer_fraction"] == 0.0
    assert cols["AircraftTotalTime"]["fill_rate"] == round(2 / 3, 4)
    assert cols["DifficultyDate"]["shapes"] == {"date_mdy": 3}
    assert cols["SubmissionDate"]["shapes"] == {"iso_datetime": 3}
    assert cols["JASCCode"]["distinct_values"] == {"2820": 3}
    assert cols["StageOfOperationCode"]["distinct_values"] == {"CR": 3}
    assert "distinct_values" not in cols["Discrepancy"]


def test_narrative_text_never_appears_in_the_report(sample):
    rendered = json.dumps(prof.profile(sample))
    assert SENTINEL not in rendered
    assert "zorblax" not in rendered.lower()


def test_extra_and_missing_columns_and_bad_width_rows_are_reported(tmp_path):
    header = [c for c in s.SDR_COLUMNS if c != "WaterLineTo"] + ["Extra77"]
    path = _write(tmp_path / "x.csv", [_row(Extra77="v")], header=header)
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write("short,row\r\n")
    report = prof.profile(path)
    cc = report["answers"]["column_count"]
    assert cc["header_columns"] == 76
    assert cc["missing_from_file"] == ["WaterLineTo"]
    assert cc["extra_in_file"] == ["Extra77"]
    assert cc["order_matches_expected"] is True
    assert report["rows_with_wrong_column_count"] == 1
    assert report["data_rows"] == 2
    assert report["columns"]["Extra77"]["expected_kind"] is None


def test_max_rows_limits_the_profile(sample):
    assert prof.profile(sample, max_rows=1)["data_rows"] == 1


def test_a_latin1_file_is_reported_not_crashed(tmp_path):
    path = _write(tmp_path / "l1.csv", [_row(Discrepancy="caf\u00e9")], encoding="latin-1")
    enc = prof.profile(path)["answers"]["encoding"]
    assert enc["latin-1"] == 0 and enc["utf-8"] >= 1


@pytest.mark.parametrize("name", ["iso_datetime", "date_mdy", "decimal", "digits", "alpha"])
def test_shape_classes(name):
    examples = {
        "iso_datetime": "2026-01-02T14:09:41.140-05:00",
        "date_mdy": "01/02/2026",
        "decimal": "1.25",
        "digits": "12345",
        "alpha": "CR",
    }
    assert prof.shape(examples[name]) == name
    assert prof.shape("AB-12") == "other"
    assert prof.shape("AB12") == "alnum"


def test_missing_and_empty_files_fail_closed(tmp_path, capsys):
    assert prof.main([str(tmp_path / "nope.csv")]) == 2
    assert "FAILED CLOSED" in capsys.readouterr().err
    empty = tmp_path / "empty.csv"
    empty.write_bytes(b"")
    assert prof.main([str(empty)]) == 2
    header_only = tmp_path / "blank.csv"
    header_only.write_bytes(b"\r\n")
    assert prof.main([str(header_only)]) == 2


def test_cli_writes_report_and_prints_answers(sample, tmp_path, capsys):
    out = tmp_path / "reports" / "profile.json"
    assert prof.main([str(sample), "--out", str(out), "--max-rows", "2"]) == 0
    captured = capsys.readouterr().out
    assert f"wrote {out}" in captured
    assert '"column_count"' in captured
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["data_rows"] == 2
    assert SENTINEL not in out.read_text(encoding="utf-8")
