"""The offline note: gate numbers are the same for any agent. Scripted agents only, no model."""

from __future__ import annotations

import pytest

from tripwire import gate_determined as gd
from tripwire.faults import FaultKind


def _sample(slots, per_fault=1, clean=2):
    """A few slots per fault plus some clean ones: enough to exercise every path, fast."""
    out, seen = [], {}
    for slot, task in slots:
        key = slot.fault.value if slot.fault else "clean"
        limit = clean if slot.fault is None else per_fault
        if seen.get(key, 0) < limit:
            seen[key] = seen.get(key, 0) + 1
            out.append((slot, task))
    return out


@pytest.fixture(scope="module")
def summary():
    v1 = _sample(gd.v1_slots())
    v2 = _sample(gd.v2_slots())
    return gd.tabulate(gd.run_all(v1, v2))


def test_every_computed_claim_holds_on_a_sample(summary):
    assert gd.checks(summary["table"]) == dict.fromkeys(gd.checks(summary["table"]), True)


def test_the_crosscheck_column_is_the_same_whatever_the_agent_does(summary):
    v2 = summary["table"]["v2-plan"]
    for fault in ("F1", "F3", "F7", "F8", "F2", "F4", "F6", "F9"):
        assert {gd.POLICIES[i]: v2[p][fault]["crosscheck"] for i, p in enumerate(gd.POLICIES)} == (
            dict.fromkeys(gd.POLICIES, 1.0)
        )
    # the contract-only gate adds nothing on value faults: it is the agent's own rate
    assert v2["wrong"]["F3"]["contract"] == v2["wrong"]["F3"]["agent"] == 0.0
    # F10 is gate-blind: every gate is the agent
    assert all(v2[p]["F10"]["crosscheck"] == v2[p]["F10"]["agent"] for p in gd.POLICIES)


def test_the_downgrade_removes_value_flag_false_alarms_only(summary):
    clean = summary["table"]["v2-plan"]
    assert clean["value_flagger"]["clean"]["agent"] == 1.0
    assert clean["value_flagger"]["clean"]["crosscheck"] < 1.0  # turned into "ok" by the oracle
    assert clean["contract_flagger"]["clean"]["crosscheck"] == 1.0  # a non-value flag survives


def test_blanket_value_flagging_scores_as_correct_ungated(summary):
    """A reviewer's point, pinned: naming every value code is scored correct on any value fault."""
    v2 = summary["table"]["v2-plan"]
    assert all(v2["value_flagger"][f]["agent"] == 1.0 for f in ("F1", "F3", "F7", "F8"))


def test_v1_live_gate_matches_the_shadow(summary):
    v1 = summary["table"]["v1-holdout"]
    assert all(cell["gate"] == cell["crosscheck"] for p in v1.values() for cell in p.values())


def test_wrong_answer_is_never_the_truth():
    slot, task = gd.v2_slots()[0]
    assert gd.final_answer("wrong", task, gd.tasks_v2.default_store) != gd.final_answer(
        "oracle", task, gd.tasks_v2.default_store
    )
    with pytest.raises(ValueError, match="unknown policy"):
        gd.final_answer("nope", task, gd.tasks_v2.default_store)


def test_invariant_groups_and_render(summary):
    inv = gd.invariant_groups(summary["table"], "crosscheck")
    assert set(inv["v2-plan F1"].values()) == {1.0}
    md = gd.render(summary)
    assert md.startswith("# The gate's numbers follow from the code")
    assert "| F1 |" in md and "100 / 100 / 100 / 100" in md and "**NO**" not in md


def test_cli_writes_the_note(tmp_path, monkeypatch):
    small = gd.run_all(_sample(gd.v1_slots(), clean=1), _sample(gd.v2_slots(), clean=1),
                       policies=("oracle", "wrong"))  # fmt: skip
    monkeypatch.setattr(gd, "run_all", lambda: small)
    out, js = tmp_path / "n.md", tmp_path / "n.json"
    assert gd.main(["--out", str(out), "--json", str(js)]) == 0
    assert "## v2-plan" in out.read_text() and '"table"' in js.read_text()


def test_value_faults_are_the_store_class():
    assert {gd.planned_class(f) for f in gd.VALUE_FAULTS} == {"store"}
    assert gd.planned_class(FaultKind.F10) == "blind"
