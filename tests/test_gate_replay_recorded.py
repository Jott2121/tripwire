"""Issue #44: pin the before/after numbers to the recorded runs, when the artifacts are present.

The receipts live in GitHub Actions artifacts (gitignored), so this test skips in CI and anywhere
they have not been downloaded. It is the committed, reproducible check behind the PR's numbers:

    gh run download 38253007798 --dir .receipts/38253007798   # baseline + prompt_fix
    gh run download 38285916667 --dir .receipts/38285916667   # gate
    python -m pytest tests/test_gate_replay_recorded.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tripwire import gate_replay as gr

RC = Path(".receipts")
PRIOR = RC / "38253007798/live-eval-paid-38253007798/run"
GATE = RC / "38285916667/live-eval-paid-38285916667/run"

pytestmark = pytest.mark.skipif(
    not (PRIOR / "receipt.jsonl").is_file() or not (GATE / "receipt.jsonl").is_file(),
    reason="recorded run artifacts 38253007798 / 38285916667 not downloaded (gitignored)",
)


def _gate(rows, resamples=2000):
    return gr.gate_cells(rows, resamples=resamples, seed=1)


def test_before_reproduces_the_issue_table():
    before, _after, _ = gr.replay(PRIOR, GATE)
    g = _gate(before)
    assert g["ALL"]["published"]["value"] == pytest.approx(0.942, abs=0.003)
    assert g["F3"]["published"]["value"] == pytest.approx(0.875, abs=0.003)
    assert g["clean"]["published"]["value"] == pytest.approx(0.091, abs=0.003)


def test_after_beats_prompt_fix_and_cuts_false_flags():
    before, after, _ = gr.replay(PRIOR, GATE)
    b, a = _gate(before), _gate(after)
    # every non-excluded faulted run is now blocked and named: published goes to 100%
    assert a["ALL"]["published"]["value"] == 1.0
    assert a["F1"]["published"]["value"] == 1.0 and a["F3"]["published"]["value"] == 1.0
    # strict floor improves, not regresses; exclusions are unchanged by the gate tweak
    assert a["ALL"]["strict"]["value"] > b["ALL"]["strict"]["value"]
    assert a["ALL"]["excluded"] == b["ALL"]["excluded"]
    # the clean false-flag rate is eliminated
    assert b["clean"]["published"]["value"] > 0.0
    assert a["clean"]["published"]["value"] == 0.0


@pytest.mark.parametrize("fold", ["A", "B"])
def test_improvement_holds_on_the_held_out_split(fold):
    before, after, _ = gr.replay(PRIOR, GATE, fold=fold)
    b, a = _gate(before), _gate(after)
    assert a["ALL"]["published"]["value"] == 1.0
    assert a["ALL"]["strict"]["value"] >= b["ALL"]["strict"]["value"]
    assert a["clean"]["published"]["value"] == 0.0
