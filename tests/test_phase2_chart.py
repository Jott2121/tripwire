"""scripts/make_phase2_chart.py: the phase two figure, built only from reports/phase2_results.json.

``test_committed_svg_matches_generator`` is a golden test: it regenerates the SVG from the
committed results and fails if the committed copy differs. The output is pure-stdlib and
deterministic, so CI reproduces identical bytes.
"""

import copy
import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "make_phase2_chart", ROOT / "scripts" / "make_phase2_chart.py"
)
mpc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mpc)


def _summary(flagged, correct, false_alarm):
    return {
        "runs": 200,
        "summary": {
            "mined": {"n": 100, "flagged": flagged, "correct": correct},
            "controls": {"n": 100, "false_alarm": false_alarm},
        },
    }


FAKE = {
    "models": {
        "grok-4.7": {
            "conditions": {
                "baseline": _summary(0.9, 0.1, 0.05),
                "prompt_fix": _summary(1.0, 0.5, 0.25),
            }
        },
        "muse-spark-open": {
            "conditions": {
                "baseline": _summary(0.8, 0.0, 0.0),
                "prompt_fix": _summary(0.95, 0.07, 0.0),
            }
        },
    }
}


def _write(root: pathlib.Path, results) -> None:
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / "phase2_results.json").write_text(json.dumps(results))


def test_pct_rounds_and_fails_closed():
    assert mpc.pct(0.97) == "97%"
    assert mpc.pct(0.0) == "0%"
    assert mpc.pct(1.0) == "100%"
    with pytest.raises(ValueError):
        mpc.pct(1.2)
    with pytest.raises(ValueError):
        mpc.pct(-0.01)


def test_extract_reads_every_rate_in_order():
    rows = mpc.extract(FAKE)
    assert [(r["model"], r["condition"]) for r in rows] == [
        ("grok-4.7", "baseline"),
        ("grok-4.7", "prompt_fix"),
        ("muse-spark-open", "baseline"),
        ("muse-spark-open", "prompt_fix"),
    ]
    assert rows[1] == {
        "model": "grok-4.7",
        "condition": "prompt_fix",
        "flagged": 1.0,
        "correct": 0.5,
        "false_alarm": 0.25,
    }


def test_extract_fails_closed_on_missing_condition():
    broken = copy.deepcopy(FAKE)
    del broken["models"]["muse-spark-open"]["conditions"]["prompt_fix"]
    with pytest.raises(KeyError):
        mpc.extract(broken)


def test_extract_refuses_wrong_item_counts():
    broken = copy.deepcopy(FAKE)
    broken["models"]["grok-4.7"]["conditions"]["baseline"]["summary"]["controls"]["n"] = 99
    with pytest.raises(ValueError, match="100 \\+ 100"):
        mpc.extract(broken)


def test_svg_shows_every_number_from_the_results(tmp_path):
    _write(tmp_path, FAKE)
    svg = mpc.render(tmp_path)
    for label in ("90%", "10%", "50%", "7%", "95%", "80%"):
        assert f">{label}</text>" in svg
    assert "false alarms on controls: 25%" in svg
    assert "false alarms on controls: 5%" in svg
    assert "800 runs" in svg
    assert "Muse Spark Open" in svg
    assert "&quot;" not in svg  # quotes in labels stay literal text, escaped only where needed
    assert svg.startswith("<svg ") and svg.endswith("</svg>\n")


def test_render_is_deterministic(tmp_path):
    _write(tmp_path, FAKE)
    assert mpc.render(tmp_path) == mpc.render(tmp_path)


def test_main_writes_the_artifact(tmp_path, capsys):
    _write(tmp_path, FAKE)
    assert mpc.main(["--root", str(tmp_path)]) == 0
    out = tmp_path / "artifacts" / "phase2_noticed_vs_acted.svg"
    assert out.read_text() == mpc.render(tmp_path)
    assert "zero API spend" in capsys.readouterr().out


def test_committed_svg_matches_generator():
    committed = (ROOT / mpc.OUT).read_text()
    assert committed == mpc.render(ROOT), (
        "artifacts/phase2_noticed_vs_acted.svg is stale; run `make demo` and commit the result"
    )
