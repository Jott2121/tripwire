import pytest

from tripwire.sdr import SDR_COLUMNS
from tripwire.synth import make_sdr_rows, write_sdr_csv


def test_rows_have_the_full_documented_header():
    # The brief's prose says "77 columns", but the ordered header it provides
    # lists 76 field names; SDR_COLUMNS reproduces that ordered header exactly.
    # The loader maps by column name and reads the live file's own header, so a
    # real file with an extra column still loads (see docs/DATA.md).
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
