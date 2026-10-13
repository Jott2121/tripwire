"""Amendment A4 holdout-1 plan: fresh, fixed, capped; criteria applied exactly."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tripwire import holdout, live_eval, runner, targets
from tripwire.prompts import Condition

ROOT = Path(__file__).resolve().parents[1]
COMMITTED = ROOT / "runs" / "targets" / "holdout-1.json"


def test_committed_plan_verifies_and_matches_regeneration():
    doc = json.loads(COMMITTED.read_text())
    assert holdout.verify_document(doc) == tuple(doc["job_ids"])
    assert doc["counts"] == {"slots": 24, "jobs": 72}


def test_plan_shape_is_stratified_and_three_conditions():
    js = holdout.jobs()
    assert len(js) == 72
    slots = {j.plan for j in js}
    by = {}
    for p in slots:
        by.setdefault(p.fault.value if p.fault else "clean", []).append(p)
    assert {k: len(v) for k, v in by.items()} == {
        "F1": 3, "F2": 3, "F3": 3, "F4": 3, "F5": 3, "F6": 3, "clean": 6,
    }  # fmt: skip
    for v in by.values():
        assert len({p.task_id for p in v}) == len(v)  # one slot per task per stratum
    assert {j.condition for j in js} == set(Condition)
    assert all(j.job_id.startswith(holdout.PREFIX) for j in js)


def _holdout_spent() -> bool:
    ledger_ids = holdout._all_ledger_run_ids(ROOT / "runs" / "spend_ledger.jsonl")
    return {j.job_id for j in holdout.jobs()} <= ledger_ids


def test_fresh_against_ledger_and_benchmark_plan():
    ledger_ids = holdout._all_ledger_run_ids(ROOT / "runs" / "spend_ledger.jsonl")
    if _holdout_spent():
        # holdout-1 is spent (run 38434106473): every job is in the ledger, none partially
        assert {j.job_id for j in holdout.jobs()} <= ledger_ids
    else:
        assert not {j.job_id for j in holdout.jobs()} & ledger_ids
    triples = holdout._benchmark_triples()
    assert not any(
        (p.task_id, p.fault, p.fault_seed) in triples for p in {j.plan for j in holdout.jobs()}
    )


def test_estimate_fits_the_cap():
    doc = json.loads(COMMITTED.read_text())
    assert doc["est_cost_usd"] == pytest.approx(72 * 0.075)
    assert doc["est_cost_usd"] <= holdout.BUDGET_USD_CAP == 6.0


def test_stale_or_overbudget_documents_are_refused():
    doc = json.loads(COMMITTED.read_text())
    with pytest.raises(targets.TargetError):
        holdout.verify_document({**doc, "job_ids": doc["job_ids"][:-1]})
    with pytest.raises(targets.TargetError):
        holdout.verify_document({**doc, "budget_usd_cap": 7.0})
    with pytest.raises(targets.TargetError):
        holdout.verify_document({**doc, "plan_seed": 1})


def test_build_refuses_a_job_already_in_the_ledger(tmp_path):
    led = tmp_path / "l.jsonl"
    rid = holdout.jobs()[0].job_id
    led.write_text('{"kind":"header"}\n' + json.dumps({"kind": "spend", "run_id": rid}) + "\n")
    with pytest.raises(targets.TargetError, match="already in the spend ledger"):
        holdout.build_document(led)


def test_cli_check_and_out(tmp_path, capsys):
    assert holdout.main(["--check", str(COMMITTED)]) == 0
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert holdout.main(["--check", str(bad)]) == 1
    out = tmp_path / "h.json"
    if _holdout_spent():
        # re-authoring a spent holdout from the real ledger refuses
        with pytest.raises(targets.TargetError, match="already in the spend ledger"):
            holdout.main(["--out", str(out)])
        assert not out.exists()
        return
    assert holdout.main(["--out", str(out)]) == 0
    assert out.read_text() == COMMITTED.read_text()
    capsys.readouterr()


def _rep(g_s, pf_s, g_p, g_ff, pf_ff, g_cost, pf_cost, records=72):
    def f(cond, s, p, cost):
        cost_cell = {"value": cost}
        return {"condition": cond, "fault": "ALL", "strict": {"value": s},
                "published": {"value": p}, "published_cost_per_correct": cost_cell}  # fmt: skip

    def c(cond, v):
        return {"condition": cond, "fault": "clean", "strict": {"value": v}}

    cells = [f("gate", g_s, g_p, g_cost), f("prompt_fix", pf_s, 0.8, pf_cost),
             c("gate", g_ff), c("prompt_fix", pf_ff)]  # fmt: skip
    return {"cells": cells, "records": records, "incomplete": []}


def test_evaluate_pass_fail_and_inconclusive():
    ok = holdout.evaluate(_rep(0.90, 0.75, 0.95, 0.0, 1 / 6, 0.07, 0.07))
    assert ok["verdict"] == "PASS" and all(ok["checks"].values())
    edge = holdout.evaluate(_rep(0.85, 0.75, 0.90, 1 / 6, 1 / 6, 0.0875, 0.07))
    assert edge["verdict"] == "PASS"  # every threshold is inclusive
    assert holdout.evaluate(_rep(0.84, 0.75, 0.95, 0.0, 0.0, 0.07, 0.07))["verdict"] == "FAIL"
    assert not holdout.evaluate(_rep(0.9, 0.7, 0.89, 0, 0, 0.07, 0.07))["checks"][
        "P2_gate_published"
    ]
    assert not holdout.evaluate(_rep(0.9, 0.7, 0.95, 2 / 6, 0.5, 0.07, 0.07))["checks"][
        "P3_clean_false_flags"
    ]
    assert not holdout.evaluate(_rep(0.9, 0.7, 0.95, 1 / 6, 0.0, 0.07, 0.07))["checks"][
        "P3_clean_false_flags"
    ]
    assert not holdout.evaluate(_rep(0.9, 0.7, 0.95, 0, 0, 0.09, 0.07))["checks"]["P4_cost_ratio"]
    assert holdout.evaluate(_rep(0.9, 0.7, 0.95, 0, 0, 0.07, 0.07, 71))["verdict"] == "INCONCLUSIVE"
    with pytest.raises(targets.TargetError):
        holdout.evaluate({"cells": [], "records": 0, "incomplete": []})


def _args(tmp_path, *extra):
    return ["--condition-set", "holdout-1", "--confirm", live_eval.CONFIRM_PHRASE,
            "--ledger-rows", "1000", "--dry-run", "--out", str(tmp_path / "o"),
            "--repo", str(ROOT), "--spend-ledger", str(ROOT / "runs" / "spend_ledger.jsonl"),
            *extra]  # fmt: skip


def test_driver_refuses_over_cap_and_mixed_flags(tmp_path, capsys):
    assert live_eval.main(_args(tmp_path, "--budget-usd", "6.5")) == runner.ExitCode.REFUSED
    assert "holdout-1 needs budget" in capsys.readouterr().err
    assert live_eval.main(_args(tmp_path, "--budget-usd", "5", "--pilot-jobs", "12")) == 2
    assert "holdout-1 excludes" in capsys.readouterr().err


def test_driver_dry_run_plans_exactly_the_holdout_jobs(tmp_path):
    if _holdout_spent():
        # a spent holdout must never be re-planned against the real ledger
        assert live_eval.main(_args(tmp_path, "--budget-usd", "5.8")) != 0
        return
    assert live_eval.main(_args(tmp_path, "--budget-usd", "5.8")) == 0
    rows = [json.loads(x) for x in (tmp_path / "o" / "receipt.jsonl").read_text().splitlines()]
    assert sorted(r["run_id"] for r in rows) == sorted(j.job_id for j in holdout.jobs())
    meta = json.loads((tmp_path / "o" / "meta.json").read_text())
    assert meta["targets"] == "holdout-1" and meta["amendment"] == "A4"
