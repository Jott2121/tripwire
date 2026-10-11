"""tripwire.synthdata and tripwire.public_specs: deterministic, contract-valid, cited."""

import json
import re
from datetime import timedelta

import pytest

from tripwire import contract as c
from tripwire import public_specs as ps
from tripwire import standards as s
from tripwire import synthdata as sd
from tripwire.contract import ContractError, SpecLimit, canonical_json, fingerprint


def test_same_seed_is_byte_identical_and_different_seed_differs():
    a, b, c = sd.generate(7), sd.generate(7), sd.generate(8)
    assert canonical_json(a) == canonical_json(b)
    assert fingerprint(a) != fingerprint(c)
    assert a.seed == 7 and len(a.defects) == 30 and len(a.lots) == 12 and len(a.work_orders) == 5


def test_sizes_are_honoured_and_negative_sizes_rejected():
    ds = sd.generate(1, n_defects=3, n_lots=2, n_work_orders=9)
    assert (len(ds.defects), len(ds.lots), len(ds.test_results)) == (3, 2, 2)
    assert len(ds.work_orders) == 3  # capped at the number of defects
    assert sd.generate(1, 0, 0, 0).defects == ()
    with pytest.raises(ValueError):
        sd.generate(1, n_defects=-1)


def test_defects_follow_the_measurement_standards():
    for d in sd.generate(3).defects:
        cn = s.parse_control_number(d.defect_id)
        assert cn.designator == sd.DESIGNATOR and cn.submitted == d.submission_date.date()
        assert s.parse_n_number(d.registry, prefix=False)
        assert d.jasc_code in s.JASC_TITLES.codes
        assert d.aircraft_total_time.unit is s.Unit.HOUR
        assert float(d.aircraft_total_time.value).is_integer()  # whole hours
        assert d.aircraft_total_cycles.unit is s.Unit.CYCLE
        assert (d.crack_length is not None) == (d.part_condition == "CRACKED")
        if d.crack_length is not None:
            assert d.crack_length.unit is s.Unit.INCH
        assert d.provenance.revision == "seed=3" and d.provenance.source_url == sd.GENERATOR_URL
        assert d.submission_date.utcoffset() == timedelta(hours=-5)  # as in real files


def test_synthetic_specs_have_a_current_revision_and_a_looser_superseded_one():
    ds = sd.generate(5)
    synth = [sp for sp in ds.spec_limits if sp.spec_id.startswith("TW-SRM-")]
    assert len(synth) == 2 * len(sd.PARTS)
    by_id: dict[str, dict[str, SpecLimit]] = {}
    for sp in synth:
        by_id.setdefault(sp.spec_id, {})[sp.revision] = sp
    for revs in by_id.values():
        assert set(revs) == {"r1", "r2"}
        assert revs["r1"].superseded_by == "r2" and revs["r2"].is_current
        assert revs["r1"].maximum.value > revs["r2"].maximum.value
        assert revs["r2"].maximum.unit is s.Unit.INCH


def test_test_results_judge_against_the_current_revision_and_mix_units():
    ds = sd.generate(11, n_lots=40)
    specs = {(sp.spec_id, sp.revision): sp for sp in ds.spec_limits}
    units = {t.measured.unit for t in ds.test_results}
    assert units == {s.Unit.INCH, s.Unit.MILLIMETRE}
    verdicts = [t.judge(specs[(t.spec_id, t.spec_revision)]) for t in ds.test_results]
    assert True in verdicts and False in verdicts
    lot_ids = {lot.lot_id for lot in ds.lots}
    assert all(t.lot_id in lot_ids for t in ds.test_results)
    with pytest.raises(ContractError):  # the superseded revision is refused
        t = ds.test_results[0]
        t.judge(specs[(t.spec_id, "r1")])


def test_work_orders_reference_existing_defects():
    ds = sd.generate(2)
    ids = {d.defect_id for d in ds.defects}
    assert all(
        w.defect_id in ids and w.created_at > ds.defects[0].submission_date
        for w in ds.work_orders[:1]
    )
    assert all(w.defect_id in ids for w in ds.work_orders)


def test_no_real_names_only_fictional_vocabulary():
    text = canonical_json(sd.generate(4)).upper()
    for word in sd.MAKES + sd.SUPPLIERS:
        assert word in text
    assert "BOEING" not in text and "AIRBUS" not in text


def test_public_spec_rows_are_the_full_table_cited_to_the_official_pdf():
    limits = ps.public_spec_limits()
    assert len(limits) == len(ps.PUBLIC_SPEC_ROWS) == 11
    by_gauge = {sp.applies_to: sp for sp in limits}
    assert set(by_gauge) == {f"AWG {g}" for g in (28, 26, 24, 22, 20, 18, 16, 14, 12, 10, 8)}
    assert by_gauge["AWG 26"].minimum == c.Quantity(36.0, s.Unit.NEWTON)
    assert by_gauge["AWG 28"].minimum == c.Quantity(22.0, s.Unit.NEWTON)
    assert by_gauge["AWG 8"].minimum.value == 1281.0
    for row, sp in zip(ps.PUBLIC_SPEC_ROWS, limits, strict=True):
        assert row.confidence is s.Confidence.HIGH and row.verified_against_pdf is True
        assert sp.source_url == s.SRC_NASA_8739_4A and sp.minimum.unit is s.Unit.NEWTON
        assert "verified_against_pdf=true" in sp.provenance.revision
        assert f"pdf_sha256={ps.NASA_8739_4A_PDF_SHA256}" in sp.provenance.revision
        assert ps.NASA_8739_4A_PDF_SHA256 in row.note and len(ps.NASA_8739_4A_PDF_SHA256) == 64
        assert sp.is_current and sp.parameter == "crimp_tensile_min"
        # the pound figure agrees with the newton value via the exact factor (rounding only)
        lb = int(re.search(r"'\d+ \((\d+)\)'", row.note).group(1))
        assert abs(s.convert(lb, s.Unit.POUND_FORCE, s.Unit.NEWTON) - row.minimum_value) < 1.5
    assert not hasattr(ps, "NASA_8739_4A_SECONDARY")  # only the official PDF is cited


def test_public_spec_row_fails_closed_without_citation_or_with_false_verification():
    with pytest.raises(ContractError):
        ps.PublicSpecRow("X", "r", "p", "a", 1.0, s.Unit.NEWTON, "", s.Confidence.HIGH, False, "")
    with pytest.raises(ContractError):
        ps.PublicSpecRow(
            "X", "r", "p", "a", 1.0, s.Unit.NEWTON, "https://x", s.Confidence.MEDIUM, True, ""
        )


def test_cli_writes_canonical_json(tmp_path, capsys):
    out = tmp_path / "runs" / "synth.json"
    assert sd.main(["--seed", "1", "--out", str(out), "--defects", "2", "--lots", "1"]) == 0
    assert f"wrote {out}" in capsys.readouterr().out
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["seed"] == 1 and len(data["defects"]) == 2 and len(data["lots"]) == 1
    assert out.read_text(encoding="utf-8") == canonical_json(sd.generate(1, 2, 1, 5)) + "\n"
    assert sd.main(["--seed", "1", "--defects", "1", "--lots", "0", "--work-orders", "0"]) == 0
    assert '"seed":1' in capsys.readouterr().out
