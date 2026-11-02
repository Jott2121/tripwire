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
    assert rep["mining"]["counts"]["S1"] >= 1 and rep["mining"]["version"] == "mine-v2"
    assert rep["mining"]["aircraft"]["S1"] <= rep["mining"]["counts"]["S1"]
    ser = rep["serials"]
    assert sum(ser["relation"].values()) == rep["sdr"]["found_in_registry"]
    assert ser["relation"]["differ"] >= 1 and ser["sdr_digits_only"] >= 0
    assert rep["sdr"]["with_total_time"] <= rep["sdr"]["rows"]
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


def test_from_zip_writes_the_same_files_and_names_the_source(tmp_path, capsys):
    fr = _script("fetch_registry")
    data = _zip({"ReleasableAircraft/MASTER.txt": "m", "ACFTREF.txt": "a"})
    z = tmp_path / "ReleasableAircraft.zip"
    z.write_bytes(data)
    net = tmp_path / "net"
    fr.fetch(net, opener=lambda url: _Resp(data), now=datetime(2026, 11, 1, tzinfo=UTC))
    out = fr.fetch_from_zip(z, tmp_path / "manual", now=datetime(2026, 11, 1, tzinfo=UTC))
    man, ref = json.loads(out.read_text()), json.loads((net / "manifest/registry.json").read_text())
    assert man.pop("source_file") == "ReleasableAircraft.zip" and man == ref
    assert (tmp_path / "manual/raw/registry/MASTER.txt").read_text() == "m"
    args = ["--from-zip", str(z), "--dest-dir", str(tmp_path / "cli")]
    assert fr.main(args) == 0 and "wrote" in capsys.readouterr().out


def test_from_zip_refuses_a_missing_or_bad_file(tmp_path, capsys):
    fr = _script("fetch_registry")
    assert fr.main(["--from-zip", str(tmp_path / "nope.zip")]) == 2
    assert "no such file" in capsys.readouterr().err
    bad = tmp_path / "page.zip"
    bad.write_bytes(b"<html>403</html>")
    assert fr.main(["--from-zip", str(bad), "--dest-dir", str(tmp_path)]) == 2
    assert "not a zip" in capsys.readouterr().err
    assert not (tmp_path / "raw").exists()


def test_peek_prints_a_sample_and_writes_nothing(real_shaped, tmp_path, capsys):
    pr = _script("profile_registry")
    master, ref, sdr = real_shaped
    base = ["--master", str(master), "--acftref", str(ref), "--sdr", str(sdr)]
    assert pr.main([*base, "--peek", "S1", "--peek-n", "5"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("rule\t") and lines[1].startswith("S1\t") and len(lines) <= 6
    assert "OTHER-SERIAL" in "\n".join(lines) and not list(tmp_path.glob("*.json"))
    marks = [ln.split("\t")[2] for ln in lines[1:]]
    assert len(marks) == len(set(marks))  # at most one item per aircraft
    with pytest.raises(SystemExit):
        pr.main([*base, "--peek", "Z9"])
    capsys.readouterr()


def _freeze_args(real_shaped, tmp_path, items_dir, **extra):
    master, ref, sdr = real_shaped
    args = [
        "--master",
        str(master),
        "--acftref",
        str(ref),
        "--sdr",
        str(sdr),
        "--out",
        str(tmp_path / "freeze.json"),
        "--items",
        str(items_dir / "items.json"),
    ]
    return args + [a for k, v in extra.items() for a in (f"--{k.replace('_', '-')}", v)]


def test_freeze_writes_counts_to_out_and_rows_only_to_items(real_shaped, tmp_path, capsys,
                                                             monkeypatch):  # fmt: skip
    from tripwire import mining, phase2

    fz = _script("freeze_phase2")
    monkeypatch.setattr(phase2, "MIN_SETTLED_AIRCRAFT", 1)
    items_dir = tmp_path / "data" / "phase2"
    args = _freeze_args(real_shaped, tmp_path, items_dir,
                        expect_fingerprint=mining.fingerprint())  # fmt: skip
    assert fz.main(args, items_dir=items_dir) == 0
    rec = json.loads((tmp_path / "freeze.json").read_text())
    assert rec["rules"] == {"version": "mine-v2", "fingerprint": mining.fingerprint()}
    assert rec["plan"]["salt"] == phase2.PLAN_SALT and rec["plan"]["n_settled"] == 100
    assert rec["settled"]["tasks"] >= 1 and rec["settled"]["aircraft"] == rec["settled"]["tasks"]
    assert rec["controls"]["aircraft"] == rec["controls"]["tasks"]
    assert rec["tasks"] == rec["settled"]["tasks"] + rec["controls"]["tasks"]
    master, _, sdr = real_shaped
    assert rec["inputs"]["master"]["sha256"] == hashlib.sha256(master.read_bytes()).hexdigest()
    assert rec["inputs"]["sdr"][0]["name"] == "SDR-2026.csv"
    out_text = (tmp_path / "freeze.json").read_text()
    assert "OTHER-SERIAL" not in out_text and "REDACTED" not in out_text
    items = json.loads((items_dir / "items.json").read_text())
    assert "OTHER-SERIAL" in json.dumps(items)  # real values stay in the local file only
    tasks = [phase2.make_task(mining.Item(**t["item"])) for t in items["tasks"]]
    assert phase2.item_list_sha256(tasks) == rec["item_list_sha256"] == items["item_list_sha256"]
    first = rec["item_list_sha256"]
    assert fz.main(args, items_dir=items_dir) == 0  # same files, same salt: same list
    assert json.loads((tmp_path / "freeze.json").read_text())["item_list_sha256"] == first
    capsys.readouterr()


def test_freeze_fails_closed(real_shaped, tmp_path, capsys):
    fz = _script("freeze_phase2")
    items_dir = tmp_path / "data" / "phase2"
    # The synthetic files give far fewer than 60 settled aircraft: the re-scope bar refuses.
    assert fz.main(_freeze_args(real_shaped, tmp_path, items_dir), items_dir=items_dir) == 2
    assert "re-scope before freeze" in capsys.readouterr().err
    bad_fp = _freeze_args(real_shaped, tmp_path, items_dir, expect_fingerprint="0" * 64)
    assert fz.main(bad_fp, items_dir=items_dir) == 2
    assert "fingerprint" in capsys.readouterr().err
    outside = _freeze_args(real_shaped, tmp_path, tmp_path / "elsewhere")
    assert fz.main(outside, items_dir=items_dir) == 2
    assert "must be under" in capsys.readouterr().err
    bad = tmp_path / "bad.txt"
    bad.write_text("NOPE\n")
    args = _freeze_args(real_shaped, tmp_path, items_dir)
    args[1] = str(bad)
    assert fz.main(args, items_dir=items_dir) == 2
    assert "REFUSED" in capsys.readouterr().err
    assert not (tmp_path / "freeze.json").exists() and not items_dir.exists()
