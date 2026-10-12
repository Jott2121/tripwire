"""scripts/make_results_chart.py: cost chart + summary built from the committed spend ledger.

The ``test_committed_artifacts_match_generator`` case is a golden test: it regenerates the two
``artifacts/`` files from ``runs/spend_ledger.jsonl`` and fails if the committed copies differ,
rewriting them so a re-run passes. The output is pure-stdlib and deterministic, so CI reproduces
identical bytes and stays green.
"""

import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "make_results_chart", ROOT / "scripts" / "make_results_chart.py"
)
mrc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mrc)

_LEDGER = (
    '{"created": "2026-10-11T23:45:00+00:00", "kind": "header", "version": 1}\n'
    '{"kind": "spend", "run_id": "T01-r01.grok-4.7.client_side_functions.baseline.k1", '
    '"cost_usd": 0.01, "timestamp": "2026-10-12T00:00:00+00:00"}\n'
    '{"kind": "spend", "run_id": "T02-r01.grok-4.7.client_side_functions.baseline.k1", '
    '"cost_usd": 0.03, "timestamp": "2026-10-12T00:01:00+00:00"}\n'
    '{"kind": "spend", "run_id": "T01-r01.grok-4.7.client_side_functions.prompt_fix.k1", '
    '"cost_usd": 0.05, "timestamp": "2026-10-12T00:02:00+00:00"}\n'
    "\n"  # blank lines are skipped
)


def _write_ledger(root: pathlib.Path, text: str = _LEDGER) -> pathlib.Path:
    (root / "runs").mkdir(parents=True, exist_ok=True)
    path = root / "runs" / "spend_ledger.jsonl"
    path.write_text(text)
    return path


def test_load_spend_skips_header_and_blanks(tmp_path):
    rows = mrc.load_spend(_write_ledger(tmp_path))
    assert len(rows) == 3
    assert all(r["kind"] == "spend" for r in rows)


def test_instant_orders_by_moment_not_text(tmp_path):
    # Same date; lexically the "-05:00" row sorts first, but by instant it is three hours later.
    text = (
        '{"kind": "spend", "run_id": "T1.grok-4.7.client_side_functions.baseline.k1", '
        '"cost_usd": 0.01, "timestamp": "2026-10-12T12:00:00+00:00"}\n'
        '{"kind": "spend", "run_id": "T2.grok-4.7.client_side_functions.baseline.k1", '
        '"cost_usd": 0.01, "timestamp": "2026-10-12T09:00:00-05:00"}\n'
    )
    agg = mrc.aggregate(mrc.load_spend(_write_ledger(tmp_path, text)))
    assert agg["first_timestamp"] == "2026-10-12T12:00:00+00:00"  # 12:00Z, the earlier instant
    assert agg["last_timestamp"] == "2026-10-12T09:00:00-05:00"  # 14:00Z, the later instant
    assert mrc._instant("not-a-timestamp") == float("inf")  # bad stamps sort last, no crash


def test_condition_of_parses_and_falls_back():
    assert mrc._condition_of("T01-r01.grok-4.7.client_side_functions.prompt_fix.k1") == "prompt_fix"
    assert mrc._condition_of("T01.grok-4.7.client_side_functions.gate.k2") == "gate"
    assert mrc._condition_of("a.b.c.d") == "c"  # unknown token -> parts[-2]


def test_aggregate_counts_and_means(tmp_path):
    agg = mrc.aggregate(mrc.load_spend(_write_ledger(tmp_path)))
    assert agg["runs"] == 3
    assert agg["conditions"]["baseline"]["runs"] == 2
    assert agg["conditions"]["baseline"]["mean_usd"] == pytest.approx(0.02)
    assert agg["conditions"]["prompt_fix"]["mean_usd"] == pytest.approx(0.05)
    assert agg["total_usd"] == pytest.approx(0.09)
    assert agg["model"] == "grok-4.7"
    assert agg["path"] == "client_side_functions"


def test_aggregate_fails_closed_on_empty():
    with pytest.raises(ValueError):
        mrc.aggregate([])


def test_build_svg_is_deterministic_and_well_formed(tmp_path):
    agg = mrc.aggregate(mrc.load_spend(_write_ledger(tmp_path)))
    svg = mrc.build_svg(agg)
    assert svg == mrc.build_svg(agg)  # deterministic
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert "Baseline" in svg and '"Check your data" prompt' in svg
    assert "$0.0200" in svg and "$0.0500" in svg  # mean labels


def test_build_summary_is_sorted_json_with_newline(tmp_path):
    agg = mrc.aggregate(mrc.load_spend(_write_ledger(tmp_path)))
    text = mrc.build_summary(agg)
    assert text.endswith("\n")
    assert json.loads(text)["runs"] == 3


def test_main_writes_both_artifacts(tmp_path, capsys):
    _write_ledger(tmp_path)
    rc = mrc.main(["--root", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "artifacts" / "results_cost.svg").is_file()
    assert (tmp_path / "artifacts" / "results_summary.json").is_file()
    assert "zero API spend" in capsys.readouterr().out


def test_committed_artifacts_match_generator():
    """Golden test: the committed artifacts must equal a fresh regeneration from the ledger."""
    agg = mrc.generate(ROOT)
    svg = mrc.build_svg(agg)
    summary = mrc.build_summary(agg)
    svg_path = ROOT / "artifacts" / "results_cost.svg"
    json_path = ROOT / "artifacts" / "results_summary.json"
    stale = (
        not svg_path.exists()
        or not json_path.exists()
        or svg_path.read_text() != svg
        or json_path.read_text() != summary
    )
    if stale:
        (ROOT / "artifacts").mkdir(parents=True, exist_ok=True)
        svg_path.write_text(svg)
        json_path.write_text(summary)
        pytest.fail(
            "Regenerated artifacts/ from the ledger; re-run pytest to confirm they are committed."
        )
