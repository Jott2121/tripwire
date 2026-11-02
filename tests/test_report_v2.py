"""v2 report: coverage classes checked against ledgers, C1-C6, Holm, verdict per model. No model."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime

import pytest

from tripwire import live_v2, runner, tasks_v2
from tripwire import report_v2 as rv
from tripwire.faults import FaultKind
from tripwire.ledger import Ledger
from tripwire.live_eval import CONFIRM_PHRASE
from tripwire.report import ReportError

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
LABEL = "grok-4.7"
PLAN = tasks_v2.default_plan()
EVENT = {  # (tool, field) a ledger records for each planned class
    "store": ("get_spec", "spec.maximum.value"),
    "contract": ("query_defects", "defects"),
    "blind": ("query_defects", "defects[0].discrepancy"),
}


def _adj(correct, false_flag=False):
    return {"correct": correct, "false_flag": false_flag, "flagged": correct, "acted": correct}


def _hit(slot, cond, share):
    return int(hashlib.sha256(f"{slot}:{cond}".encode()).hexdigest(), 16) % 100 < share * 100


def make_run(root, *, gate_fixes=True, gate_false_flags=0, slots=PLAN, exit_name="OK"):
    """A scored v2 directory: baseline right on ~50% of faulted slots, prompt_fix on ~60%; the
    crosscheck shadow fixes every catchable slot, the contract shadow only the contract class."""
    ledgers = root / "ledgers"
    ledgers.mkdir(parents=True)
    rows, flagged = [], 0
    for slot in slots:
        fault = slot.fault
        klass = rv.planned_class(fault) if fault else None
        for cond, share in (("baseline", 0.5), ("prompt_fix", 0.6)):
            run_id = f"{slot.run_id}.{LABEL}.client_side_functions.{cond}.k1"
            if fault is None:
                ff = cond == "prompt_fix" and flagged < gate_false_flags
                flagged += ff
                raw = _adj(True)
                shadow = {"contract": _adj(True), "crosscheck": _adj(not ff, ff)}
            else:
                led = Ledger(ledgers / f"{run_id}.jsonl")
                tool, field = EVENT[klass]
                led.append(run_id=run_id, tool=tool, fault=fault.value, field=field, original=1,
                           served=2, timestamp=NOW)  # fmt: skip
                ok = _hit(slot.run_id, cond, share)
                cross = ok or (gate_fixes and klass in rv.CATCHABLE)
                contract = ok or (gate_fixes and klass == "contract")
                raw, shadow = _adj(ok), {"contract": _adj(contract), "crosscheck": _adj(cross)}
            if fault is None:
                Ledger(ledgers / f"{run_id}.jsonl")
            rows.append({
                "run_id": run_id, "task_id": slot.task_id,
                "planned_fault": fault.value if fault else None, "condition": cond,
                "path": "client_side_functions", "model_label": LABEL, "cost_usd": 0.01,
                "adjudication": raw, "exclusion": None, "task_set": "v2",
                "shadow": {k: {"adjudication": v} for k, v in shadow.items()},
            })  # fmt: skip
    (root / "receipt.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (root / "meta.json").write_text(json.dumps({"study": "v2", "arm": "scored", "slot": "M1"}))
    (root / "result.json").write_text(json.dumps({"exit_name": exit_name}))
    return root


def _report(*dirs, resamples=200):
    rows, sources = rv.load_model(list(dirs), LABEL)
    return rv.analyse_model(rows, sources, resamples)


def test_a_model_the_gate_fixes_generalises_and_the_rest_are_inconclusive(tmp_path):
    rep = _report(make_run(tmp_path / "a"))
    assert rep["records"] == 960 and not rep["incomplete"]
    assert rep["coverage_slots"] == {"blind": 40, "clean": 80, "contract": 200, "store": 160}
    study = rv.study({LABEL: rep})
    v = study["verdicts"][LABEL]
    assert v["verdict"] == "GATE GENERALISES" and v["checks"] == {"C1": True, "C6": True}
    t = v["tests"]
    assert t["C1"]["value"] == pytest.approx(1 - _pf_catchable(rep))
    assert t["C5"]["value"] == 0 and v["notes"]["C5_within_band"]  # blind: no lift by design
    assert t["C4"]["value"] > 0.3  # crosscheck carries the store class
    assert v["holm"]["alpha"] == pytest.approx(0.05 / 4)  # three models never ran: p = 1
    assert study["passing"] == 1
    others = [m for m in rv.FAMILY if m != LABEL]
    assert all(study["verdicts"][m]["verdict"] == "INCONCLUSIVE" for m in others)


def _pf_catchable(rep):
    cell = next(c for c in rep["cells"] if c["arm"] == "prompt_fix" and c["class"] == "catchable")
    return cell["strict"]["value"]


def test_no_gate_lift_or_one_false_flag_does_not_generalise(tmp_path):
    flat = rv.study({LABEL: _report(make_run(tmp_path / "a", gate_fixes=False))})["verdicts"]
    assert flat[LABEL]["verdict"] == "DOES NOT GENERALISE" and not flat[LABEL]["checks"]["C1"]
    one = rv.study({LABEL: _report(make_run(tmp_path / "b", gate_false_flags=1))})["verdicts"]
    v = one[LABEL]
    assert v["checks"] == {"C1": True, "C6": False}  # 1 of 80: exact upper bound about 5.8%
    assert v["c6_gate_upper"] == pytest.approx(rv.binomial_upper(1, 80))


def test_a_short_or_halted_run_is_inconclusive(tmp_path):
    short = _report(make_run(tmp_path / "a", slots=PLAN[:120]))
    assert rv.study({LABEL: short})["verdicts"][LABEL]["verdict"] == "INCONCLUSIVE"
    halted = _report(make_run(tmp_path / "b", exit_name="BUDGET_HALT"))
    assert rv.study({LABEL: halted})["verdicts"][LABEL]["verdict"] == "INCONCLUSIVE"


def test_shards_load_together(tmp_path):
    a = make_run(tmp_path / "s1", slots=PLAN[:240])
    b = make_run(tmp_path / "s2", slots=PLAN[240:])
    assert _report(a, b)["records"] == 960


def _edit(root, run_id_part, **over):
    receipt = root / "receipt.jsonl"
    rows = [json.loads(x) for x in receipt.read_text().splitlines()]
    hit = next(r for r in rows if run_id_part in r["run_id"] and r["planned_fault"])
    hit.update(over)
    receipt.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return hit


def test_fail_closed_on_bad_inputs(tmp_path):
    root = make_run(tmp_path / "a", slots=PLAN[:12])
    hit = _edit(root, ".baseline.")
    (root / "ledgers" / f"{hit['run_id']}.jsonl").unlink()
    with pytest.raises(ReportError, match="no injection ledger"):
        rv.load_model([root], LABEL)
    _edit(root, ".baseline.", exclusion="RUN_ERROR", adjudication=None)
    rv.load_model([root], LABEL)  # an excluded run may have no ledger
    with pytest.raises(ReportError, match="not a gpt-oss-120b"):
        rv.load_model([root], "gpt-oss-120b")
    _edit(root, ".prompt_fix.", planned_fault="F2" if hit["planned_fault"] != "F2" else "F3")
    with pytest.raises(ReportError, match="frozen plan"):
        rv.load_model([root], LABEL)


def test_a_ledger_outside_the_planned_class_fails_closed(tmp_path):
    f5 = [s for s in PLAN if s.fault is FaultKind.F5][:1]
    root = make_run(tmp_path / "a", slots=f5)
    run_id = f"{f5[0].run_id}.{LABEL}.client_side_functions.baseline.k1"
    path = root / "ledgers" / f"{run_id}.jsonl"
    for p in root.joinpath("ledgers").glob(f"{run_id}*"):
        p.unlink()
    Ledger(path).append(run_id=run_id, tool="query_defects", fault="F5", field="defects[0].text",
                        original="ab", served="a", timestamp=NOW)  # fmt: skip
    with pytest.raises(ReportError, match="another class"):
        rv.load_model([root], LABEL)


def test_missing_shadow_and_non_scored_directories_are_refused(tmp_path):
    root = make_run(tmp_path / "a", slots=PLAN[:6])
    _edit(root, ".baseline.", shadow=None)
    with pytest.raises(ReportError, match="shadow"):
        rv.load_model([root], LABEL)
    pilot = make_run(tmp_path / "p", slots=PLAN[:6])
    (pilot / "meta.json").write_text(json.dumps({"study": "v2", "arm": "pilot"}))
    with pytest.raises(ReportError, match="not a scored"):
        rv.load_model([pilot], LABEL)


def test_holm_steps_down_and_stops_at_the_first_failure():
    out = rv.holm({"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.5})
    assert out["a"] == (True, pytest.approx(0.0125))
    assert out["c"] == (False, pytest.approx(0.05 / 3))  # 0.03 > 0.0167: stop here
    assert out["b"] == (False, pytest.approx(0.025)) and not out["d"][0]


def test_binomial_upper_matches_the_planned_bound():
    assert rv.binomial_upper(0, 80) == pytest.approx(1 - 0.05 ** (1 / 80), abs=1e-9)
    assert rv.binomial_upper(80, 80) == 1.0
    with pytest.raises(ValueError):
        rv.binomial_upper(3, 2)


def test_cli_writes_the_report_and_one_verdict_per_model(tmp_path, capsys):
    run = make_run(tmp_path / "a")
    out = tmp_path / "reports"
    assert rv.main(["--run", f"{LABEL}={run}", "--out", str(out), "--resamples", "50"]) == 0
    assert sorted(p.name for p in out.iterdir()) == sorted(
        ["v2-report.json", "v2-report.md", *(f"verdict-{m}.json" for m in rv.FAMILY)]
    )
    md = (out / "v2-report.md").read_text()
    assert "1 of 4" in md and f"## {LABEL}: GATE GENERALISES" in md and "No records." in md
    assert json.loads((out / f"verdict-{LABEL}.json").read_text())["verdict"] == "GATE GENERALISES"
    assert rv.main(["--run", f"gpt-5={run}", "--out", str(out)]) == 2
    assert "REFUSED" in capsys.readouterr().out


def test_a_real_dry_run_directory_loads(tmp_path):
    """Format check: the driver's own output is what the report reads."""
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / live_v2.PROTOCOL).write_text("**Status: DRAFT**\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "p"], check=True)
    ledger = tmp_path / "l.jsonl"
    runner.init_spend_ledger(ledger, lambda: NOW)
    out = tmp_path / "out"
    argv = ["run", "--repo", str(repo), "--spend-ledger", str(ledger), "--out", str(out),
            "--model", LABEL, "--stage", "1", "--budget-usd", "5", "--ledger-rows", "0",
            "--confirm", CONFIRM_PHRASE, "--dry-run", "--max-jobs", "8"]  # fmt: skip
    assert live_v2.main(argv, env={}) == 0
    rows, sources = rv.load_model([out], LABEL)
    rep = rv.analyse_model(rows, sources, 20)
    assert rep["records"] == 8 and rv.study({LABEL: rep})["verdicts"][LABEL]["verdict"] == (
        "INCONCLUSIVE"
    )
