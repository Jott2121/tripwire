"""fetch_registry and profile_registry on synthetic data; a fake opener, never the network."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.test_registry import ACFTREF, MASTER_HEADER, master_row
from tripwire.synth import make_sdr_rows, write_sdr_csv

ROOT = Path(__file__).resolve().parents[1] / "scripts"


def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    def __init__(self, data):
        self.data, self.closed = data, False

    def read(self):
        return self.data

    def close(self):
        self.closed = True


def _zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in members.items():
            zf.writestr(name, text)
    return buf.getvalue()


def test_fetch_extracts_only_the_named_members_to_fixed_names(tmp_path):
    fr = _script("fetch_registry")
    data = _zip({"MASTER.txt": "m", "ACFTREF.txt": "a", "../../evil.txt": "x", "DEALER.txt": "d"})
    resp = _Resp(data)
    out = fr.fetch(tmp_path, opener=lambda url: resp, now=datetime(2026, 11, 1, tzinfo=UTC))
    man = json.loads(out.read_text())
    assert (
        resp.closed and man["url"] == fr.URL and man["sha256"] == hashlib.sha256(data).hexdigest()
    )
    assert sorted(p.name for p in (tmp_path / "raw" / "registry").iterdir()) == [
        "ACFTREF.txt", "MASTER.txt",
    ]  # fmt: skip
    assert (
        man["members"]["MASTER.txt"]["bytes"] == 1 and not (tmp_path.parent / "evil.txt").exists()
    )
    assert man["retrieved_utc"].startswith("2026-11-01")


def test_fetch_fails_closed(tmp_path):
    fr = _script("fetch_registry")
    with pytest.raises(fr.FetchError, match="not a zip"):
        fr.fetch(tmp_path, opener=lambda url: _Resp(b"html"))
    with pytest.raises(fr.FetchError, match="0 members named ACFTREF"):
        fr.fetch(tmp_path, opener=lambda url: _Resp(_zip({"MASTER.txt": "m"})))


@pytest.fixture
def real_shaped(tmp_path):
    rows = make_sdr_rows(30, seed=3)
    sdr = tmp_path / "SDR-2026.csv"
    write_sdr_csv(rows, sdr)
    with sdr.open(newline="", encoding="utf-8") as f:
        parsed = list(csv.DictReader(f))
    marks = [(r["RegistryNNumber"], r["AircraftSerialNumber"]) for r in parsed]
    master_rows = [master_row(n, s, "0010101", "1990") for n, s in marks[:20]]
    master_rows[0] = master_row(marks[0][0], "OTHER-SERIAL", "0010101", "1990")
    master = tmp_path / "MASTER.txt"
    master.write_text(MASTER_HEADER + "\n" + "\n".join(master_rows) + "\nbroken,row\n")
    ref = tmp_path / "ACFTREF.txt"
    ref.write_text(ACFTREF)
    return master, ref, sdr


def test_profile_reports_counts_never_rows(real_shaped, tmp_path, capsys):
    pr = _script("profile_registry")
    master, ref, sdr = real_shaped
    out = tmp_path / "rep.json"
    args = ["--master", str(master), "--acftref", str(ref), "--sdr", str(sdr), "--out", str(out)]
    assert pr.main(args) == 0
    rep = json.loads(out.read_text())
    assert rep["master"]["rows"] == 20 and rep["master"]["reject_kinds"] == {
        "2 columns, header has 31": 1
    }
    assert rep["sdr"]["rows"] == 30 and rep["sdr"]["found_in_registry"] >= 19
    assert rep["mining"]["counts"]["S1"] >= 1 and rep["mining"]["version"] == "mine-v0"
    text = out.read_text()
    assert "OTHER-SERIAL" not in text and "REDACTED" not in text and "TEST WAY" not in text
    capsys.readouterr()


def test_profile_refuses_a_bad_file(real_shaped, tmp_path, capsys):
    pr = _script("profile_registry")
    master, ref, sdr = real_shaped
    bad = tmp_path / "bad.txt"
    bad.write_text("NOPE\n")
    assert pr.main(["--master", str(bad), "--acftref", str(ref), "--sdr", str(sdr)]) == 2
    assert "REFUSED" in capsys.readouterr().err
