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
    REPEATS,
    RUNS_PER_TASK,
    ExitCode,
    Job,
    RunnerRefused,
    assert_protocol_committed,
    plan_matrix,
    repeat_subset,
    run_matrix,
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


def _run(repo, tmp_path, jobs, client, cap=100.0, pricing=None):
    return run_matrix(
        jobs, sp.BY_ID, client, out_dir=tmp_path / "out", protocol=repo / "PREREG.md", repo=repo,
        cap_usd=cap, pricing=pricing or {sp.MODEL.label: sp.PRICE}, clock=sp.CLOCK,
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
    with pytest.raises(RunnerRefused, match="gate"):
        _run(repo, tmp_path, _jobs(1, Condition.GATE), _client(1))


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
    assert len(plan) == 210
    assert counts == {"clean": 48, "F1": 25, "F2": 14, "F3": 30, "F4": 6, "F5": 39, "F6": 48}


def test_matrix_shape_subset_repeats_and_second_path():
    subset = repeat_subset(sp.TASKS)
    assert len(subset) == 6 and subset == repeat_subset(sp.TASKS)
    jobs = plan_matrix(sp.TASKS, [sp.MODEL])
    assert len({j.job_id for j in jobs}) == len(jobs)
    in_sub = [j for j in jobs if j.plan.task_id in subset]
    assert {j.path for j in jobs if j.plan.task_id not in subset} == {ExecPath.CLIENT_SIDE}
    assert {j.path for j in in_sub} == set(ExecPath)
    assert {j.repeat for j in in_sub} == set(range(1, REPEATS + 1))
    assert {j.condition for j in jobs} == set(Condition)
