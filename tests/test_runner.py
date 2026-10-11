"""Runner: protocol refusal, budget cap, resume, quota halt, matrix shape. Scripted client only."""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from dataclasses import replace

import pytest
import support as sp

from tripwire import tasks as tk
from tripwire import xai
from tripwire.prompts import Condition
from tripwire.runner import (
    CLEAN_SHARE,
    CONDITIONS,
    EST_INPUT_TOKENS,
    EST_OUTPUT_TOKENS,
    FIRST_RUN_BUDGET_USD,
    HARD_CEILING_USD,
    REPEATS,
    RUNS_PER_TASK,
    ExitCode,
    Job,
    RunnerRefused,
    SpendLedgerError,
    assert_protocol_committed,
    estimate_run_cost,
    init_spend_ledger,
    plan_matrix,
    read_spend,
    run_matrix,
    server_subset,
    wilson_interval,
)
from tripwire.xai import ExecPath

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def _git(repo, *args):
    subprocess.run([*GIT, "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "PREREG.md").write_text("protocol v1\n")
    _git(root, "add", "PREREG.md")
    _git(root, "commit", "-qm", "protocol")
    return root


def _jobs(n, condition=Condition.BASELINE, model=sp.MODEL):
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    return [
        Job(tk.PlannedRun(f"{task.task_id}-r{i:02d}", task.task_id, None, i),
            model, ExecPath.CLIENT_SIDE, condition, 1)
        for i in range(1, n + 1)
    ]  # fmt: skip


def _client(n, tokens=(10, 5)):
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    return sp.client_for(
        *(
            sp.agent_script(task, ExecPath.CLIENT_SIDE, sp.truth_json(task), tokens)
            for _ in range(n)
        )
    )


def _ledger(tmp_path):
    path = tmp_path / "spend" / "ledger.jsonl"
    if not path.exists():
        init_spend_ledger(path, sp.CLOCK)
    return path


def _run(repo, tmp_path, jobs, client, cap=100.0, pricing=None, out="out", ceiling=50.0):
    return run_matrix(
        jobs, sp.BY_ID, client, out_dir=tmp_path / out, protocol=repo / "PREREG.md", repo=repo,
        cap_usd=cap, pricing=pricing or {sp.MODEL.label: sp.PRICE}, clock=sp.CLOCK,
        spend_ledger=_ledger(tmp_path), ceiling_usd=ceiling,
    )  # fmt: skip


def _receipt(tmp_path):
    return [json.loads(x) for x in (tmp_path / "out/receipt.jsonl").read_text().splitlines()]


def test_committed_protocol_is_accepted_and_uncommitted_refused(repo, tmp_path):
    commit, digest = assert_protocol_committed(repo / "PREREG.md", repo)
    assert len(commit) == 40 and len(digest) == 64
    (repo / "PREREG.md").write_text("edited\n")
    with pytest.raises(RunnerRefused, match="uncommitted"):
        assert_protocol_committed(repo / "PREREG.md", repo)
    (repo / "NEW.md").write_text("never added\n")
    with pytest.raises(RunnerRefused, match="not committed"):
        assert_protocol_committed(repo / "NEW.md", repo)


def test_runner_refuses_an_uncommitted_protocol_and_runs_nothing(repo, tmp_path):
    (repo / "PREREG.md").write_text("edited\n")
    client = _client(1)
    with pytest.raises(RunnerRefused):
        _run(repo, tmp_path, _jobs(1), client)
    assert client.requests == [] and not (tmp_path / "out").exists()


def test_end_to_end_writes_the_receipt_layout(repo, tmp_path):
    assert _run(repo, tmp_path, _jobs(2), _client(2)) is ExitCode.OK
    out = tmp_path / "out"
    meta = json.loads((out / "meta.json").read_text())
    assert meta["git_commit"] and meta["protocol_sha256"] and meta["jobs"] == 2
    rows = _receipt(tmp_path)
    assert len(rows) == 2 and all(r["adjudication"]["correct"] for r in rows)
    result = json.loads((out / "result.json").read_text())
    assert result["exit_name"] == "OK" and result["records_total"] == 2
    assert len(list((out / "ledgers").glob("*.jsonl"))) == 2


def test_budget_cap_refuses_to_start_when_projection_exceeds_it(repo, tmp_path):
    client = _client(3)
    with pytest.raises(RunnerRefused, match="exceeds the cap"):
        _run(repo, tmp_path, _jobs(3), client, cap=2.5)  # 3 x 1.0 USD estimated
    assert client.requests == []
    with pytest.raises(RunnerRefused, match="positive"):
        _run(repo, tmp_path, _jobs(1), client, cap=0)


def test_budget_halts_mid_run_when_actual_spend_beats_the_estimate_then_resumes(repo, tmp_path):
    jobs = _jobs(2)
    first = _client(1, tokens=(20_000, 5))  # each call 1.0 USD; 2 turns = 2.0, estimate was 1.0
    assert _run(repo, tmp_path, jobs, first, cap=2.5) is ExitCode.BUDGET_HALT
    assert len(_receipt(tmp_path)) == 1
    second = _client(1)
    assert _run(repo, tmp_path, jobs, second, cap=10.0) is ExitCode.OK
    assert [r["run_id"] for r in _receipt(tmp_path)] == [j.job_id for j in jobs]  # no repeats
    assert _run(repo, tmp_path, jobs, _client(0), cap=10.0) is ExitCode.OK  # nothing left to do


def test_quota_exhaustion_halts_without_recording_the_run(repo, tmp_path):
    client = xai.ScriptedClient([xai.QuotaExhausted("quota")])
    assert _run(repo, tmp_path, _jobs(2), client) is ExitCode.QUOTA_HALT
    assert not (tmp_path / "out/receipt.jsonl").exists()
    assert json.loads((tmp_path / "out/result.json").read_text())["exit_name"] == "QUOTA_HALT"


def test_run_errors_are_recorded_and_reflected_in_the_exit_code(repo, tmp_path):
    client = xai.ScriptedClient([xai.XaiError("boom")])
    assert _run(repo, tmp_path, _jobs(1), client) is ExitCode.RUN_ERRORS
    assert _receipt(tmp_path)[0]["exclusion"] == "RUN_ERROR"


def test_unresolved_models_prices_and_the_gate_are_refused(repo, tmp_path):
    unresolved = replace(sp.MODEL, model_id=xai.UNRESOLVED)
    with pytest.raises(RunnerRefused, match="UNRESOLVED"):
        _run(repo, tmp_path, _jobs(1, model=unresolved), _client(1))
    with pytest.raises(RunnerRefused, match="UNRESOLVED"):
        _run(repo, tmp_path, _jobs(1), _client(1), pricing={sp.MODEL.label: None})
    server_gate = [replace(_jobs(1, Condition.GATE)[0], path=ExecPath.SERVER_SIDE)]
    with pytest.raises(RunnerRefused, match="gate"):
        _run(repo, tmp_path, server_gate, _client(1))


def test_gate_condition_runs_client_side_and_is_never_planned_server_side(repo, tmp_path):
    assert _run(repo, tmp_path, _jobs(1, Condition.GATE), _client(1)) is ExitCode.OK
    row = _receipt(tmp_path)[0]
    assert row["gate"]["blocked"] is False and row["adjudication"]["correct"]
    planned = plan_matrix(sp.TASKS, [sp.MODEL], server_share=0.2, conditions=tuple(Condition))
    gated = [j for j in planned if j.condition is Condition.GATE]
    assert gated and {j.path for j in gated} == {ExecPath.CLIENT_SIDE}


def test_a_receipt_directory_is_bound_to_its_protocol_version(repo, tmp_path):
    _run(repo, tmp_path, _jobs(1), _client(1))
    (repo / "PREREG.md").write_text("protocol v2\n")
    _git(repo, "commit", "-qam", "v2")
    with pytest.raises(RunnerRefused, match="different protocol"):
        _run(repo, tmp_path, _jobs(2), _client(2))


def test_realized_fault_plan_is_pinned():
    """Numbers quoted in docs/PREREGISTRATION.md; regenerate the doc if this changes."""
    plan = tk.fault_plan(sp.TASKS, 1, RUNS_PER_TASK, CLEAN_SHARE)
    counts = Counter(r.fault.value if r.fault else "clean" for r in plan)
    assert (RUNS_PER_TASK, len(plan)) == (6, 240)
    assert counts == {"clean": 55, "F1": 21, "F2": 13, "F3": 22, "F4": 20, "F5": 48, "F6": 61}


def test_lean_matrix_fits_the_first_run_budget_and_leaves_the_gate_out():
    price = xai.PRICING["grok-4.7"]
    assert (EST_INPUT_TOKENS, EST_OUTPUT_TOKENS) == (20_000, 2_000)
    assert estimate_run_cost(price) == pytest.approx(0.052)
    jobs = plan_matrix(sp.TASKS, list(xai.MODEL_CONFIGS))
    assert len(jobs) == 480  # 240 planned runs x baseline and prompt-fix
    assert len(jobs) * estimate_run_cost(price) == pytest.approx(24.96)
    assert len(jobs) * estimate_run_cost(price) <= FIRST_RUN_BUDGET_USD
    assert {j.condition for j in jobs} == {Condition.BASELINE, Condition.PROMPT_FIX}
    assert CONDITIONS == (Condition.BASELINE, Condition.PROMPT_FIX)
    assert {j.path for j in jobs} == {ExecPath.CLIENT_SIDE}  # server-side deferred
    assert {j.repeat for j in jobs} == {1} and REPEATS == 1
    assert len({j.job_id for j in jobs}) == len(jobs)
    per_cell = Counter(
        (j.condition.value, j.plan.fault.value if j.plan.fault else "clean") for j in jobs
    )
    assert set(per_cell.values()) == {55, 21, 13, 22, 20, 48, 61}  # every cell, both conditions
    assert len(per_cell) == 14


def test_matrix_shape_subset_repeats_and_second_path_when_enabled():
    subset = server_subset(sp.TASKS, 0.2)
    assert len(subset) == 8 and subset == server_subset(sp.TASKS, 0.2)
    assert server_subset(sp.TASKS) == frozenset()  # deferred by default
    jobs = plan_matrix(sp.TASKS, [sp.MODEL], server_share=0.2)
    in_sub = [j for j in jobs if j.plan.task_id in subset]
    assert {j.path for j in jobs if j.plan.task_id not in subset} == {ExecPath.CLIENT_SIDE}
    assert {j.path for j in in_sub} == set(ExecPath)
    # the gate is only planned when asked for, after M6
    with_gate = plan_matrix(sp.TASKS, [sp.MODEL], server_share=0.2, conditions=tuple(Condition))
    assert {j.condition for j in with_gate} == set(Condition)
    assert not [
        j for j in with_gate if j.condition is Condition.GATE and j.path is ExecPath.SERVER_SIDE
    ]  # M6 deviation: the gate is client-side only


def test_wilson_interval_matches_known_values():
    lo, hi = wilson_interval(10, 20)
    assert (hi - lo) / 2 == pytest.approx(0.2008, abs=1e-3)
    assert wilson_interval(0, 55)[1] == pytest.approx(0.0653, abs=1e-3)
    with pytest.raises(ValueError):
        wilson_interval(3, 2)


def _spend_rows(tmp_path):
    lines = (tmp_path / "spend/ledger.jsonl").read_text().splitlines()
    return [json.loads(x) for x in lines[1:]]


def test_spend_accumulates_across_sessions_and_output_directories(repo, tmp_path):
    assert _run(repo, tmp_path, _jobs(2), _client(2)) is ExitCode.OK
    assert _run(repo, tmp_path, _jobs(1), _client(1), out="other") is ExitCode.OK
    ledger = tmp_path / "spend/ledger.jsonl"
    assert read_spend(ledger) == pytest.approx(3 * 2 * 10 * 50 / 1e6)
    assert {r["out_dir"] for r in _spend_rows(tmp_path)} == {
        str(tmp_path / "out"),
        str(tmp_path / "other"),
    }
    result = json.loads((tmp_path / "other/result.json").read_text())
    assert result["ledger_spent_usd"] == pytest.approx(read_spend(ledger))


def test_ceiling_refuses_to_start_on_prior_spend_plus_projection(repo, tmp_path):
    _run(repo, tmp_path, _jobs(1), _client(1, tokens=(20_000, 5)))  # spends 2.0 USD
    client = _client(2)
    with pytest.raises(RunnerRefused, match="cumulative ceiling"):
        _run(repo, tmp_path, _jobs(2), client, out="other", ceiling=3.5)  # 2.0 + 2 x 1.0 > 3.5
    assert client.requests == [] and not (tmp_path / "other").exists()


def test_ceiling_halts_mid_run_before_the_job_that_would_cross_it(repo, tmp_path):
    first = _client(1, tokens=(20_000, 5))  # 2.0 USD actual vs a 1.0 USD estimate
    assert _run(repo, tmp_path, _jobs(2), first, ceiling=2.5) is ExitCode.BUDGET_HALT
    assert len(_receipt(tmp_path)) == 1 and len(_spend_rows(tmp_path)) == 1
    other = _client(1)
    with pytest.raises(RunnerRefused, match="cumulative ceiling"):  # 2.0 spent + 1.0 > 2.5
        _run(repo, tmp_path, _jobs(1), other, out="other", ceiling=2.5)
    assert other.requests == []  # a different directory is stopped by the shared ledger


def test_ceiling_can_only_be_lowered(repo, tmp_path):
    assert HARD_CEILING_USD == 50.0
    for bad in (50.01, 0, -1):
        with pytest.raises(RunnerRefused, match="ceiling"):
            _run(repo, tmp_path, _jobs(1), _client(1), ceiling=bad)


def test_provider_ticks_are_preferred_in_the_spend_ledger(repo, tmp_path):
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    first, last = sp.agent_script(task, ExecPath.CLIENT_SIDE, sp.truth_json(task))
    ticks = 25 * 10**8  # 0.25 USD, against 0.001 USD from the token estimate
    client = xai.ScriptedClient([replace(first, cost_in_usd_ticks=ticks), last])
    assert _run(repo, tmp_path, _jobs(1), client) is ExitCode.OK
    (row,) = _spend_rows(tmp_path)
    assert row["cost_in_usd_ticks"] == ticks
    assert row["cost_usd"] == pytest.approx(0.25 + 10 * 50 / 1e6)
    assert _receipt(tmp_path)[0]["cost_in_usd_ticks"] == ticks


@pytest.mark.parametrize(
    "content",
    [
        "",  # empty file
        "not json\n",
        '{"kind": "spend", "cost_usd": 1.0}\n',  # no header
        '{"kind": "header"}\n{"kind": "spend", "cost_usd": "1"}\n',
        '{"kind": "header"}\n{"kind": "spend", "cost_usd": -1}\n',
        '{"kind": "header"}\n{"kind": "spend", "cost_usd": NaN}\n',
        '{"kind": "header"}\n{"kind": "spend", "cost_usd": 1.0\n',  # torn write
        '{"kind": "header"}\n\n',  # blank line
    ],
)
def test_corrupt_spend_ledger_fails_closed(repo, tmp_path, content):
    ledger = _ledger(tmp_path)
    ledger.write_text(content)
    client = _client(1)
    with pytest.raises(SpendLedgerError):
        _run(repo, tmp_path, _jobs(1), client)
    assert client.requests == [] and ledger.read_text() == content  # untouched, not reset


def test_missing_spend_ledger_is_refused_not_created_as_zero(repo, tmp_path):
    client = _client(1)
    with pytest.raises(SpendLedgerError, match="unreadable"):
        run_matrix(
            _jobs(1), sp.BY_ID, client, out_dir=tmp_path / "out", protocol=repo / "PREREG.md",
            repo=repo, cap_usd=100.0, pricing={sp.MODEL.label: sp.PRICE}, clock=sp.CLOCK,
            spend_ledger=tmp_path / "nowhere.jsonl",
        )  # fmt: skip
    assert client.requests == [] and not (tmp_path / "nowhere.jsonl").exists()
    created = tmp_path / "fresh.jsonl"
    init_spend_ledger(created, sp.CLOCK)
    assert read_spend(created) == 0.0
    with pytest.raises(FileExistsError):  # an existing ledger is never reset
        init_spend_ledger(created, sp.CLOCK)


def test_resume_after_a_quota_halt_parks_the_stale_ledger_and_finishes(repo, tmp_path):
    jobs = _jobs(2)
    halted = xai.ScriptedClient([xai.QuotaExhausted("quota")])
    assert _run(repo, tmp_path, jobs, halted) is ExitCode.QUOTA_HALT
    stale = tmp_path / "out/ledgers" / f"{jobs[0].job_id}.jsonl"
    assert stale.exists()  # run_one created it before the first call
    assert _run(repo, tmp_path, jobs, _client(2)) is ExitCode.OK
    assert [r["run_id"] for r in _receipt(tmp_path)] == [j.job_id for j in jobs]
    parked = tmp_path / "out/ledgers/superseded"
    assert (parked / f"{jobs[0].job_id}.1.jsonl").exists()
    assert (parked / f"{jobs[0].job_id}.1.jsonl.head").exists()
    assert stale.exists()  # the rerun wrote a fresh one


def test_repeated_halts_never_overwrite_parked_ledgers(repo, tmp_path):
    jobs = _jobs(1)
    for _ in range(2):
        client = xai.ScriptedClient([xai.QuotaExhausted("q")])
        assert _run(repo, tmp_path, jobs, client) is ExitCode.QUOTA_HALT
    assert _run(repo, tmp_path, jobs, _client(1)) is ExitCode.OK
    names = sorted(p.name for p in (tmp_path / "out/ledgers/superseded").glob("*.jsonl"))
    assert names == [f"{jobs[0].job_id}.1.jsonl", f"{jobs[0].job_id}.2.jsonl"]


def test_meta_keeps_the_first_session_and_appends_each_resume(repo, tmp_path):
    jobs = _jobs(2)
    _run(repo, tmp_path, jobs[:1], _client(1))
    meta_path = tmp_path / "out/meta.json"
    first = json.loads(meta_path.read_text())
    _git(repo, "commit", "-q", "--allow-empty", "-m", "unrelated")  # HEAD moves on
    run_matrix(
        jobs, sp.BY_ID, _client(1), out_dir=tmp_path / "out", protocol=repo / "PREREG.md",
        repo=repo, cap_usd=100.0, pricing={sp.MODEL.label: sp.PRICE},
        clock=lambda: sp.NOW.replace(year=2027), spend_ledger=_ledger(tmp_path),
    )  # fmt: skip
    meta = json.loads(meta_path.read_text())
    assert meta["started"] == first["started"] and meta["git_commit"] == first["git_commit"]
    assert len(meta["sessions"]) == 2
    assert meta["sessions"][0]["started"] == first["started"]
    assert meta["sessions"][1]["started"].startswith("2027")
    assert meta["sessions"][1]["git_commit"] != first["git_commit"]
