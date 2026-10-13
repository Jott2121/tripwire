"""Deterministic gate-confirm-1 target set (issue #46). No network, no real spend."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tripwire import runner, targets, xai

ROOT = Path(__file__).resolve().parent.parent
COMMITTED = ROOT / "runs" / "targets" / "gate-confirm-1.json"


def _ledger(path: Path, *rows: dict) -> Path:
    lines = [json.dumps({"kind": "header", "version": 1})]
    lines += [json.dumps(r) for r in rows]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_committed_file_is_a_valid_bounded_gate_set():
    doc = json.loads(COMMITTED.read_text())
    targets.verify_document(doc)  # raises on any inconsistency
    assert doc["condition_set"] == "gate"
    assert doc["counts"] == {
        "already_run": 186, "plan": 240, "skipped": 54, "topup": 6, "total": 60,
    }  # fmt: skip
    assert doc["est_cost_usd"] <= targets.BUDGET_USD_CAP
    assert doc["budget_usd_cap"] == 4.0
    assert len(doc["job_ids"]) == 60 == len(set(doc["job_ids"]))


def test_committed_file_matches_regeneration_from_the_committed_ledger():
    assert COMMITTED.read_text() == targets.render_document()


def test_check_cli_passes_on_the_committed_file(capsys):
    assert targets.main(["--check", str(COMMITTED)]) == 0
    assert "ok:" in capsys.readouterr().out


def test_out_cli_authors_from_the_real_ledger(tmp_path, capsys):
    out = tmp_path / "gate-confirm-1.json"
    assert targets.main(["--out", str(out)]) == 0
    assert "wrote" in capsys.readouterr().out
    assert out.read_text() == COMMITTED.read_text()


def test_build_document_splits_skipped_and_stratified_topup():
    plan = targets.gate_plan()
    assert len(plan) == 240
    ran = frozenset(j.job_id for j in plan[:186])  # pretend these were paid for
    doc = targets.build_document(plan, ran)
    assert doc["counts"] == {
        "already_run": 186, "plan": 240, "skipped": 54, "topup": 6, "total": 60,
    }  # fmt: skip
    by_id = {j.job_id: j for j in plan}
    # every topup job is one of the strata the #45 change touches, and nothing double-counts
    assert all(targets._fault_label(by_id[j]) in targets.TOPUP_FAULTS for j in doc["topup_job_ids"])
    assert not set(doc["skipped_job_ids"]) & set(doc["topup_job_ids"])
    assert set(doc["job_ids"]) == set(doc["skipped_job_ids"]) | set(doc["topup_job_ids"])
    # job_ids follow plan order
    order = [j.job_id for j in plan]
    assert doc["job_ids"] == [jid for jid in order if jid in set(doc["job_ids"])]
    # the stratified topup is deterministic
    assert targets.build_document(plan, ran)["job_ids"] == doc["job_ids"]


def test_build_refuses_when_nothing_was_skipped():
    plan = targets.gate_plan()
    ran = frozenset(j.job_id for j in plan)
    with pytest.raises(targets.TargetError, match="nothing to confirm"):
        targets.build_document(plan, ran)


def test_build_refuses_unknown_ran_id_and_oversized_target():
    plan = targets.gate_plan()
    with pytest.raises(targets.TargetError, match="not in the current plan"):
        targets.build_document(plan, frozenset({"nope"}))
    with pytest.raises(targets.TargetError, match="exceed target"):
        targets.build_document(plan, frozenset(), target_total=1)  # 240 skipped > 1


def test_build_refuses_when_estimate_would_exceed_cap():
    plan = targets.gate_plan()
    # 240 skipped jobs * $0.052 = $12.48, over the $4 cap
    with pytest.raises(targets.TargetError, match="exceeds"):
        targets.build_document(plan, frozenset(), target_total=240)


def test_ran_gate_job_ids_only_counts_gate_spend_rows(tmp_path):
    gate = "T01-r01.grok-4.7.client_side_functions.gate.k1"
    base = "T01-r01.grok-4.7.client_side_functions.baseline.k1"
    led = _ledger(
        tmp_path / "l.jsonl",
        {"kind": "spend", "run_id": gate, "cost_usd": 1},
        {"kind": "spend", "run_id": base, "cost": 1},  # not a gate row
        {"kind": "header", "run_id": "T02.x.gate.k1"},  # not a spend row
    )
    assert targets.ran_gate_job_ids(led) == frozenset({gate})


def test_ran_gate_job_ids_fails_closed(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"kind": "header"}\nnot json\n')
    with pytest.raises(targets.TargetError, match="corrupt"):
        targets.ran_gate_job_ids(bad)
    with pytest.raises(targets.TargetError, match="unreadable"):
        targets.ran_gate_job_ids(tmp_path / "missing.jsonl")


def _doc(plan, skipped, topup=()):
    ids = [j.job_id for j in plan]
    chosen = set(skipped) | set(topup)
    return {
        "name": "t", "issue": 46, "condition_set": "gate", "source_run": "x",
        "budget_usd_cap": 4.0, "est_cost_usd": 0.0, "selection": "s",
        "counts": {}, "skipped_job_ids": list(skipped), "topup_job_ids": list(topup),
        "job_ids": [i for i in ids if i in chosen],
    }  # fmt: skip


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda d: d.update(condition_set="baseline-prompt-fix"), "condition_set must be"),
        (lambda d: d.update(job_ids=[]), "empty"),
        (lambda d: d["job_ids"].append(d["job_ids"][0]), "duplicates"),
        (lambda d: d["job_ids"].append("ghost-job"), "not in the gate plan"),
    ],
)
def test_verify_rejects_bad_documents(mutate, match):
    plan = targets.gate_plan()
    doc = _doc(plan, [j.job_id for j in plan[:3]])
    mutate(doc)
    with pytest.raises(targets.TargetError, match=match):
        targets.verify_document(doc, plan)


def test_verify_rejects_overlap_and_mismatched_union_and_bad_topup():
    plan = targets.gate_plan()
    first3 = [j.job_id for j in plan[:3]]
    overlap = _doc(plan, first3, first3)  # same ids in both lists
    with pytest.raises(targets.TargetError, match="overlap"):
        targets.verify_document(overlap, plan)
    mism = _doc(plan, first3)
    mism["job_ids"] = first3[:2]  # job_ids != skipped + topup
    with pytest.raises(targets.TargetError, match="not exactly skipped"):
        targets.verify_document(mism, plan)
    # a topup job whose planted fault is not F1/F3/clean (e.g. a write task, F4/F5/F6)
    by_id = {j.job_id: j for j in plan}
    non = next(j for j in plan if targets._fault_label(j) not in targets.TOPUP_FAULTS)
    bad = _doc(plan, [], [non.job_id])
    assert by_id[non.job_id] is non
    with pytest.raises(targets.TargetError, match="not F1/F3/clean"):
        targets.verify_document(bad, plan)


def test_verify_rejects_estimate_over_cap():
    plan = targets.gate_plan()
    big = [j.job_id for j in plan[:80]]  # 80 * $0.052 = $4.16 > $4
    with pytest.raises(targets.TargetError, match="exceeds"):
        targets.verify_document(_doc(plan, big), plan)


def test_load_targets_returns_ids_and_fails_closed(tmp_path):
    plan = targets.gate_plan()
    good = _doc(plan, [j.job_id for j in plan[:3]])
    path = tmp_path / "t.json"
    path.write_text(json.dumps(good))
    assert targets.load_targets(path, "gate", plan) == tuple(good["job_ids"])
    with pytest.raises(targets.TargetError, match="condition-set"):
        targets.load_targets(path, "baseline-prompt-fix", plan)


def test_check_cli_reports_invalid(tmp_path, capsys):
    bad = tmp_path / "b.json"
    bad.write_text(json.dumps({"condition_set": "gate", "job_ids": []}))
    assert targets.main(["--check", str(bad)]) == 1
    assert "invalid:" in capsys.readouterr().err


def test_estimate_matches_the_runner_cost():
    plan = targets.gate_plan()
    one = targets._estimate_usd([plan[0].job_id], plan)
    assert one == pytest.approx(runner.estimate_run_cost(xai.PRICING["grok-4.7"]))


def test_estimate_fails_closed_on_an_unpriced_model(monkeypatch):
    plan = targets.gate_plan()
    monkeypatch.setattr(xai, "PRICING", {})  # no price for any model
    with pytest.raises(targets.TargetError, match="no price for model"):
        targets.estimate_jobs_usd(plan[:1])


def test_verify_rejects_desynced_counts():
    plan = targets.gate_plan()
    doc = _doc(plan, [j.job_id for j in plan[:3]])
    doc["counts"] = {"total": 2, "skipped": 3, "topup": 0}  # total disagrees with the 3 job_ids
    with pytest.raises(targets.TargetError, match="counts block disagrees"):
        targets.verify_document(doc, plan)
