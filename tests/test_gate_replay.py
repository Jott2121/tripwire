"""Offline gate replay (issue #44): faithful re-adjudication from recorded receipts + ledgers."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tripwire import contract as c
from tripwire import gate_replay as gr
from tripwire import standards as s
from tripwire import tasks as tk
from tripwire.adjudicator import Adjudication
from tripwire.faults import FaultKind
from tripwire.ledger import Ledger

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
STORE = tk.default_store()
BY_ID = {t.task_id: t for t in tk.default_tasks()}


def _job(task_id: str, rep: int = 1) -> str:
    return f"{task_id}-r{rep:02d}.flagship-high.client_side_functions.gate.k1"


def _write_ledger(ledger_dir: Path, run_id: str, events) -> None:
    ledger_dir.mkdir(parents=True, exist_ok=True)
    led = Ledger(ledger_dir / f"{run_id}.jsonl")
    for fault, field, original, served in events:
        led.append(run_id=run_id, tool="x", fault=fault, field=field, original=original,
                   served=served, timestamp=NOW)  # fmt: skip


def _adj(injected=(), *, correct=False, false_flag=False, flagged=False, acted=False) -> dict:
    n = len(injected)
    return asdict(Adjudication(
        "r", tuple(injected), flagged, acted, correct, (), false_flag, n, n,
        (n / n if n else None),
    ))  # fmt: skip


def _rec(run_id, task_id, *, planned_fault, condition="gate", exclusion=None, verdict=None,
         adjudication=None, gate=None, tool_calls=()) -> dict:  # fmt: skip
    return {
        "run_id": run_id,
        "task_id": task_id,
        "planned_fault": planned_fault,
        "condition": condition,
        "path": "client_side_functions",
        "model_label": "flagship-high",
        "model_requested": "m",
        "effort": "medium",
        "served_models": ["m"],
        "input_tokens": 10,
        "output_tokens": 5,
        "cost_usd": 0.05,
        "cost_in_usd_ticks": None,
        "latency_s": 0.1,
        "timestamp": NOW.isoformat(),
        "turns": 2,
        "tool_calls": list(tool_calls),
        "verdict": verdict,
        "adjudication": adjudication,
        "exclusion": exclusion,
        "error": None,
        "turn_cap_hit": False,
        "gate": gate,
    }


def _open_gate(**over) -> dict:
    base = {"checks_run": 1, "quantity_checks": 0, "failures": [], "blocked": False,
            "downgraded": False, "extra_tool_calls": 0, "latency_s": 0.0,
            "read_back_faults": [], "added_tokens": 0, "agent_verdict": None}  # fmt: skip
    return {**base, **over}


def test_ratio_fault_matches_the_unit_based_classifier():
    """The replay's ratio classifier and the gate's unit classifier agree on every case."""
    from tripwire.gate import classify_value_fault

    for (src, _dst), factor in s.CONVERSION_FACTORS.items():
        assert gr.ratio_fault(2.5, 2.5 * factor) is FaultKind.F1
        assert classify_value_fault(c.Quantity(2.5, src), c.Quantity(2.5 * factor, src)) is (
            FaultKind.F1
        )
        assert gr.ratio_fault(2.5, 2.5 / factor) is FaultKind.F1
    for wrong in (0.8, 0.9, 1.1, 1.25):
        assert gr.ratio_fault(2.0, 2.0 * wrong) is FaultKind.F3
    assert gr.ratio_fault(1.0, 1.0) is None
    assert gr.ratio_fault(0, 5) is FaultKind.F3  # no ratio: a changed value is a wrong value
    assert gr.ratio_fault("x", 5) is None and gr.ratio_fault(True, 1) is None


def test_fold_tasks_is_a_fixed_index_split():
    tasks = tk.default_tasks()
    a, b = gr._fold_tasks(tasks, "A"), gr._fold_tasks(tasks, "B")
    assert a.isdisjoint(b) and a | b == {t.task_id for t in tasks}
    assert gr._fold_tasks(tasks, "all") == a | b
    with pytest.raises(gr.ReplayError):
        gr._fold_tasks(tasks, "nonsense")


def test_coerce_query_args_parses_since_and_drops_blanks():
    assert gr._coerce_query_args({"since": "2026-01-02", "part_number": "P"}) == {
        "since": tk.date(2026, 1, 2),
        "part_number": "P",
    }
    assert gr._coerce_query_args({"since": "", "registry": None, "jasc_code": "2820"}) == {
        "jasc_code": "2820"
    }


def _spec_task(fault):
    return next(t for t in BY_ID.values() if t.kind is tk.TaskKind.SPEC_LIMIT and fault in t.faults)


def test_replay_catches_a_missed_f3_and_scores_it_correct(tmp_path):
    task = _spec_task(FaultKind.F3)
    rid = _job(task.task_id)
    store_max = STORE.get_spec(*task.key).maximum
    served = round(store_max.value * 0.9, 4)
    _write_ledger(tmp_path, rid, [("F3", "spec.maximum.value", store_max.value, served)])
    row = _rec(rid, task.task_id, planned_fault="F3", tool_calls=["get_spec"],
               verdict={"status": "ok", "value": served, "unit": store_max.unit.value,
                        "work_order_id": None, "flags": []},
               adjudication=_adj(("F3",)), gate=_open_gate())  # fmt: skip
    out = gr.replay_gate_row(row, STORE, BY_ID, tmp_path)
    assert out["gate"]["blocked"] and out["verdict"]["status"] == "blocked"
    assert out["verdict"]["flags"] == ["wrong_value"]
    assert out["adjudication"]["correct"] and out["adjudication"]["flagged"]


def test_replay_catches_a_missed_f1_with_no_parseable_verdict(tmp_path):
    task = _spec_task(FaultKind.F1)
    rid = _job(task.task_id)
    factor = next(iter(s.CONVERSION_FACTORS.values()))
    _write_ledger(tmp_path, rid, [("F1", "spec.maximum.value", 2.0, 2.0 * factor)])
    row = _rec(
        rid,
        task.task_id,
        planned_fault="F1",
        tool_calls=["get_spec"],
        verdict=None,
        adjudication=_adj(("F1",)),
        gate=_open_gate(),
    )
    out = gr.replay_gate_row(row, STORE, BY_ID, tmp_path)
    assert out["verdict"]["flags"] == ["unit_swap"] and out["adjudication"]["correct"]


def test_replay_downgrades_a_clean_value_refusal(tmp_path):
    task = next(t for t in BY_ID.values() if t.kind is tk.TaskKind.SPEC_LIMIT)
    rid = _job(task.task_id)
    _write_ledger(tmp_path, rid, [])  # a clean run has an (empty) ledger
    row = _rec(rid, task.task_id, planned_fault=None, tool_calls=["get_spec"],
               verdict={"status": "rejected", "value": None, "unit": None, "work_order_id": None,
                        "flags": ["F3", "wrong_value"]},
               adjudication=_adj((), false_flag=True, acted=True), gate=_open_gate())  # fmt: skip
    out = gr.replay_gate_row(row, STORE, BY_ID, tmp_path)
    assert out["gate"]["downgraded"] and out["gate"]["quantity_checks"] >= 1
    assert out["verdict"]["status"] == "ok" and out["verdict"]["flags"] == []
    assert not out["adjudication"]["false_flag"]
    assert out["gate"]["agent_verdict"]["status"] == "rejected"  # pre-gate verdict kept


def test_replay_leaves_an_already_caught_fault_and_excluded_runs_untouched(tmp_path):
    f6 = next(
        t for t in BY_ID.values() if t.kind is tk.TaskKind.SPEC_LIMIT and FaultKind.F6 in t.faults
    )
    rid = _job(f6.task_id)
    _write_ledger(tmp_path, rid, [("F6", "spec.provenance", {"a": 1}, None)])
    blocked_verdict = {"status": "blocked", "value": None, "unit": None, "work_order_id": None,
                       "flags": ["dropped_field"]}  # fmt: skip
    adj = _adj(("F6",), correct=True, flagged=True, acted=True)
    fail = [{"check": "field_missing", "fault": "F6", "detail": "x"}]
    row = _rec(rid, f6.task_id, planned_fault="F6", verdict=blocked_verdict, adjudication=adj,
               gate=_open_gate(blocked=True, failures=fail))  # fmt: skip
    out = gr.replay_gate_row(row, STORE, BY_ID, tmp_path)
    assert out["verdict"] == blocked_verdict and out["adjudication"] == adj  # unchanged

    excluded = _rec(
        _job("T27", 6),
        "T27",
        planned_fault="F5",
        condition="gate",
        exclusion="RUN_ERROR",
        verdict=None,
        adjudication=None,
        gate=_open_gate(),
    )
    assert gr.replay_gate_row(excluded, STORE, BY_ID, tmp_path) is excluded

    # a non-gate row is returned unchanged with no ledger lookup
    base = _rec(
        "x.flagship-high.client_side_functions.baseline.k1",
        "T01",
        planned_fault=None,
        condition="baseline",
        adjudication=_adj(),
    )
    assert gr.replay_gate_row(base, STORE, BY_ID, tmp_path) is base


def _mini_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """A small gate run dir (one row per task kind that matters) plus a prior comparator dir."""
    spec = _spec_task(FaultKind.F3)
    test_m = next(t for t in BY_ID.values() if t.kind is tk.TaskKind.TEST_MEASURED)
    since = next(
        t
        for t in BY_ID.values()
        if t.kind is tk.TaskKind.DEFECT_COUNT and any("since" in cc.args for cc in t.calls)
    )
    gate_dir = tmp_path / "gate" / "run"
    prior_dir = tmp_path / "prior" / "run"
    ledgers = gate_dir / "ledgers"
    gate_dir.mkdir(parents=True)
    prior_dir.mkdir(parents=True)

    gate_rows, prior_rows = [], []
    # faulted F3 spec, missed by the old gate
    rid = _job(spec.task_id)
    store_max = STORE.get_spec(*spec.key).maximum
    _write_ledger(ledgers, rid, [("F3", "spec.maximum.value", store_max.value,
                                  round(store_max.value * 0.9, 4))])  # fmt: skip
    gate_rows.append(_rec(rid, spec.task_id, planned_fault="F3", tool_calls=["get_spec"],
                          verdict={"status": "ok", "value": 1.0, "unit": store_max.unit.value,
                                   "work_order_id": None, "flags": []},
                          adjudication=_adj(("F3",)), gate=_open_gate()))  # fmt: skip
    # clean runs exercising each reconstructed read tool
    for task, tool in ((test_m, "get_test_result"), (since, "query_defects")):
        cid = _job(task.task_id)
        _write_ledger(ledgers, cid, [])
        gate_rows.append(_rec(cid, task.task_id, planned_fault=None, tool_calls=[tool],
                              verdict={"status": "ok", "value": 1.0, "unit": "count",
                                       "work_order_id": None, "flags": []},
                              adjudication=_adj(), gate=_open_gate()))  # fmt: skip
    # a comparator twin for the gate slots in each condition
    for task in (spec, test_m, since):
        for cond in ("baseline", "prompt_fix"):
            prior_rows.append(_rec(
                f"{task.task_id}-r01.flagship-high.client_side_functions.{cond}.k1",
                task.task_id, planned_fault=(None if task is test_m else "F3"), condition=cond,
                verdict={"status": "ok", "value": 1.0, "unit": "count", "work_order_id": None,
                         "flags": []}, adjudication=_adj()))  # fmt: skip
    (gate_dir / "receipt.jsonl").write_text("\n".join(json.dumps(r) for r in gate_rows) + "\n")
    (prior_dir / "receipt.jsonl").write_text("\n".join(json.dumps(r) for r in prior_rows) + "\n")
    return prior_dir, gate_dir


def test_replay_and_report_over_run_dirs(tmp_path):
    prior_dir, gate_dir = _mini_dirs(tmp_path)
    before, after, sources = gr.replay(prior_dir, gate_dir)
    assert len(before) == len(after) and sources
    bc = gr.gate_cells(before)
    ac = gr.gate_cells(after)
    # the missed F3 is correct after the replay; it was not before
    assert ac["F3"]["published"]["value"] == 1.0
    assert bc["F3"]["correct"] == 0 and ac["F3"]["correct"] == 1
    # folds partition the tasks
    fa, _, _ = gr.replay(prior_dir, gate_dir, fold="A")
    fb, _, _ = gr.replay(prior_dir, gate_dir, fold="B")
    assert len(fa) + len(fb) == len(before)


def test_main_writes_a_report(tmp_path):
    prior_dir, gate_dir = _mini_dirs(tmp_path)
    out = tmp_path / "reports"
    assert gr.main([str(prior_dir), str(gate_dir), "--out", str(out), "--name", "t",
                    "--resamples", "200"]) == 0  # fmt: skip
    assert (out / "t.md").exists() and (out / "t.json").exists()
