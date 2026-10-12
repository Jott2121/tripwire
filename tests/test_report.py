"""Report tests: fixture receipt rows only (no network, no clock, no key)."""

from __future__ import annotations

import json

import pytest

from tripwire import report as rp

FAST = {"resamples": 200, "seed": 1}
SRC = [{"dir": "d", "meta": {}, "result": {}, "records": 1}]


def row(task, fault, correct, *, cond="baseline", k=1, cost=0.05, excl=None, ff=False, gate=None):
    adj = None if excl == "RUN_ERROR" else {"correct": correct, "false_flag": ff}
    return {
        "run_id": f"{task}.{fault or 'clean'}.m.7.client_side.{cond}.k{k}",
        "task_id": task,
        "planned_fault": fault,
        "condition": cond,
        "path": "client_side",
        "model_label": "m.7",
        "cost_usd": cost,
        "adjudication": adj,
        "exclusion": excl,
        "turn_cap_hit": False,
        "gate": gate,
    }


def write(d, rows, result=None, meta=None):
    d.mkdir()
    (d / "receipt.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (d / "meta.json").write_text(json.dumps(meta or {}))
    (d / "result.json").write_text(json.dumps(result or {"exit_name": "OK"}))
    return d


def fixture_rows():
    rows = []
    for i in range(1, 9):  # 8 tasks; every clean run is correct except T8's
        t = f"T{i}"
        rows.append(row(t, None, i != 8, ff=(i == 3)))
        rows.append(row(t, "F1", i % 2 == 0))
    rows.append(row("T1", "F2", True, excl="INJECTION_UNVERIFIED"))
    rows.append(row("T2", "F2", True, excl="RUN_ERROR"))
    return rows


def cell(rep, fault):
    return next(c for c in rep["cells"] if c["fault"] == fault)


def test_bootstrap_is_deterministic_and_brackets_the_estimate():
    clusters = {f"T{i}": [(float(i % 2), 1.0)] for i in range(20)}
    a, b = rp.bootstrap_ci(clusters, 500, 3), rp.bootstrap_ci(clusters, 500, 3)
    assert a == b and a["lo"] <= 0.5 <= a["hi"] and a["valid"] == 500


def test_bootstrap_clusters_by_task_so_correlated_runs_widen_the_interval():
    same = {f"T{i}": [(float(i % 2), 1.0)] * 5 for i in range(10)}  # 5 identical runs per task
    spread = {f"T{i}": [(float(j % 2), 1.0) for j in range(5)] for i in range(10)}
    a, b = rp.bootstrap_ci(same, 500), rp.bootstrap_ci(spread, 500)
    assert a["hi"] - a["lo"] > b["hi"] - b["lo"]


def test_bootstrap_never_invents_an_interval():
    assert rp.bootstrap_ci({"T1": [(1.0, 1.0)]})["lo"] is None  # one cluster
    assert rp.bootstrap_ci({"T1": [(0.0, 0.0)], "T2": [(0.0, 0.0)]}, 50)["lo"] is None


def test_baseline_fail_is_applied_to_faulted_runs_of_a_task_whose_clean_run_failed():
    ex = rp.effective_exclusions(fixture_rows())
    assert ex["T8.F1.m.7.client_side.baseline.k1"] == "BASELINE_FAIL"
    assert ex["T1.F1.m.7.client_side.baseline.k1"] is None
    assert ex["T2.F2.m.7.client_side.baseline.k1"] == "RUN_ERROR"  # a receipt code outranks


def test_strict_floor_and_published_number_are_a_pair_from_the_records():
    rep = rp.analyse(fixture_rows(), SRC, **FAST)
    f1 = cell(rep, "F1")  # T2, T4, T6, T8 correct; T8 is BASELINE_FAIL, so it is dropped
    assert (f1["runs"], f1["kept"], f1["correct"]) == (8, 7, 3)
    assert f1["strict"]["den"] == 8 and f1["published"]["den"] == 7
    assert f1["strict"]["num"] == f1["published"]["num"] == 3
    assert f1["strict"]["value"] < f1["published"]["value"]
    assert f1["excluded"] == {"BASELINE_FAIL": 1}
    f2 = cell(rep, "F2")
    assert f2["excluded"] == {"INJECTION_UNVERIFIED": 1, "RUN_ERROR": 1}
    assert f2["published"]["value"] is None and f2["strict"]["value"] == 0.0
    assert rep["exclusions_by_code"] == {
        "BASELINE_FAIL": 1,
        "INJECTION_UNVERIFIED": 1,
        "RUN_ERROR": 1,
    }


def test_clean_false_flag_rate_and_cost_per_correct():
    rep = rp.analyse(fixture_rows(), SRC, **FAST)
    clean = cell(rep, "clean")
    assert clean["false_flags"] == 1 and clean["published"]["value"] == pytest.approx(1 / 8)
    f1 = cell(rep, "F1")
    assert f1["published_cost_per_correct"]["value"] == pytest.approx(7 * 0.05 / 3)
    assert f1["strict_cost_per_correct"]["value"] == pytest.approx(8 * 0.05 / 3)
    assert cell(rep, "F2")["published_cost_per_correct"]["value"] is None  # no correct run


def test_unknown_cost_is_reported_as_unknown_not_estimated():
    rows = fixture_rows()
    rows[1]["cost_usd"] = None
    rep = rp.analyse(rows, SRC, **FAST)
    assert cell(rep, "F1")["strict_cost_per_correct"]["value"] is None
    assert rep["spend_usd"] is None


def test_gate_overhead_pairs_with_prompt_fix_by_plan_and_repeat():
    rows = []
    g = {"extra_tool_calls": 2, "latency_s": 0.5, "added_tokens": 0}
    for i in range(1, 7):
        t = f"T{i}"
        rows.append(row(t, None, True, cond="prompt_fix", cost=0.10))
        rows.append(row(t, "F4", True, cond="prompt_fix", cost=0.10))
        rows.append(row(t, None, True, cond="gate", cost=0.11, gate=g))
        rows.append(row(t, "F4", True, cond="gate", cost=0.12, gate=g))
    rep = rp.analyse(rows, SRC, **FAST)
    go = rep["gate_overhead"]
    assert go["pairs"] == 12 and go["cost_overhead"]["value"] == pytest.approx(0.15)
    assert go["extra_tool_calls_per_run"] == 2 and go["added_latency_s_per_run"] == 0.5
    assert "Gate overhead" in rp.render_markdown(rep)


def test_gate_overhead_absent_without_gate_and_none_without_twin():
    assert rp.analyse(fixture_rows(), SRC, **FAST)["gate_overhead"] is None
    rep = rp.analyse([row("T1", None, True, cond="gate")], SRC, **FAST)
    assert rep["gate_overhead"]["cost_overhead"] is None
    assert "No gate run has a priced" in rp.render_markdown(rep)


def test_pair_key_refuses_a_foreign_run_id_layout():
    bad = row("T1", None, True, cond="gate")
    bad["run_id"] = "nonsense"
    with pytest.raises(rp.ReportError):
        rp.gate_overhead([bad])


def test_load_fails_closed(tmp_path):
    with pytest.raises(rp.ReportError, match="no receipt"):
        rp.load_records([tmp_path])
    r = row("T1", None, True)
    with pytest.raises(rp.ReportError, match="duplicate"):
        rp.load_records([write(tmp_path / "a", [r, r])])
    with pytest.raises(rp.ReportError, match="never adjudicated"):
        rp.load_records([write(tmp_path / "b", [{**r, "adjudication": None}])])
    with pytest.raises(rp.ReportError, match="lacks"):
        rp.load_records([write(tmp_path / "c", [{"run_id": "x"}])])
    with pytest.raises(rp.ReportError, match="no run records"):
        rp.load_records([write(tmp_path / "d", [])])
    e = write(tmp_path / "e", [r])
    (e / "meta.json").write_text("{not json")
    with pytest.raises(rp.ReportError, match="unreadable"):
        rp.load_records([e])


def test_cli_end_to_end_writes_markdown_and_json_and_never_overwrites(tmp_path, capsys):
    d = write(tmp_path / "run1", fixture_rows())
    out = tmp_path / "reports"
    args = [str(d), "--out", str(out), "--resamples", "200"]
    assert rp.main(args) == 0
    text = (out / "run1.md").read_text()
    assert "strict floor" in text and "false-flag" in text and "PILOT" not in text
    data = json.loads((out / "run1.json").read_text())
    assert data["params"]["resamples"] == 200 and data["records"] == len(fixture_rows())
    assert rp.main(args) == 2 and "report exists" in capsys.readouterr().out
    assert rp.main([str(d), "--out", str(out), "--name", "x", "--resamples", "5"]) == 2


def test_pilot_and_halted_runs_are_labelled(tmp_path):
    d = write(
        tmp_path / "p",
        fixture_rows(),
        result={"exit_name": "BUDGET_HALT", "pilot": True},
        meta={"pilot": True},
    )
    rows, sources = rp.load_records([d])
    text = rp.render_markdown(rp.analyse(rows, sources, **FAST))
    assert "PILOT RUN" in text and "INCOMPLETE" in text
