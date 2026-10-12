"""Resume: RUN_ERROR runs stay pending, the newest record wins, --resume-from retries only them."""

from __future__ import annotations

import json
import subprocess

import pytest
import support as sp

from tripwire import live_eval, report, runner

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_receipt_leaves_run_errors_pending_but_counts_their_spend(tmp_path):
    path = tmp_path / "receipt.jsonl"
    _write(
        path,
        [
            {"run_id": "a", "exclusion": None, "cost_usd": 1.0},
            {"run_id": "b", "exclusion": "RUN_ERROR", "cost_usd": 0.5},
            {"run_id": "c", "exclusion": "INJECTION_UNVERIFIED", "cost_usd": None},
        ],
    )
    done, spent = runner._read_receipt(path)
    assert done == {"a", "c"} and spent == pytest.approx(1.5)


def test_newest_record_decides_and_earlier_spend_still_counts(tmp_path):
    path = tmp_path / "receipt.jsonl"
    _write(
        path,
        [
            {"run_id": "b", "exclusion": "RUN_ERROR", "cost_usd": 0.5},
            {"run_id": "b", "exclusion": None, "cost_usd": 0.25},
            {"run_id": "d", "exclusion": None, "cost_usd": 1.0},
            {"run_id": "d", "exclusion": "RUN_ERROR", "cost_usd": 0.5},
        ],
    )
    done, spent = runner._read_receipt(path)
    assert done == {"b"} and spent == pytest.approx(2.25)
    assert runner.failed_run_ids(path) == {"d"}
    assert runner.failed_run_ids(tmp_path / "missing.jsonl") == set()


def test_report_keeps_only_the_newest_record_per_run_id(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    base = {k: None for k in report._REQUIRED}
    _write(
        d / "receipt.jsonl",
        [
            {**base, "run_id": "x", "exclusion": "RUN_ERROR"},
            {**base, "run_id": "y", "exclusion": "RUN_ERROR"},
            {**base, "run_id": "x", "exclusion": "INJECTION_UNVERIFIED"},
        ],
    )
    rows, sources = report.load_records([d])
    assert [(r["run_id"], r["exclusion"]) for r in rows] == [
        ("y", "RUN_ERROR"),
        ("x", "INJECTION_UNVERIFIED"),
    ]
    assert sources[0]["records"] == 2


# --- driver -----------------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "PREREGISTRATION.md").write_text("protocol v1\n")
    subprocess.run([*GIT, "-C", str(root), "init", "-q"], check=True)
    subprocess.run([*GIT, "-C", str(root), "add", "."], check=True)
    subprocess.run([*GIT, "-C", str(root), "commit", "-qm", "p"], check=True)
    return root


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "spend_ledger.jsonl"
    runner.init_spend_ledger(path, sp.CLOCK)
    return path


def _argv(repo, ledger, *extra, budget="25"):
    return [
        "--condition-set", "baseline-prompt-fix", "--budget-usd", budget,
        "--confirm", live_eval.CONFIRM_PHRASE, "--ledger-rows", "0",
        "--spend-ledger", str(ledger), "--repo", str(repo), "--dry-run", *extra,
    ]  # fmt: skip


def _first_run(repo, ledger, tmp_path):
    out = tmp_path / "out"
    assert live_eval.main(_argv(repo, ledger, "--out", str(out), "--max-jobs", "3"), env={}) == 0
    rows = [json.loads(x) for x in (out / "receipt.jsonl").read_text().splitlines()]
    rows[0]["exclusion"] = "RUN_ERROR"
    _write(out / "receipt.jsonl", rows)
    return out, rows[0]["run_id"]


def test_resume_from_reattempts_only_failed_jobs_and_counts_prior_spend(repo, ledger, tmp_path):
    out, failed = _first_run(repo, ledger, tmp_path)
    assert live_eval.main(_argv(repo, ledger, "--resume-from", str(out)), env={}) == 0
    rows = [json.loads(x) for x in (out / "receipt.jsonl").read_text().splitlines()]
    assert len(rows) == 4 and [r["run_id"] for r in rows].count(failed) == 2
    result = json.loads((out / "result.json").read_text())
    assert result["ran_this_session"] == 1 and result["records_total"] == 3
    assert result["spent_usd"] == pytest.approx(4 * 0.052)  # three prior runs + the retry
    assert live_eval.main(_argv(repo, ledger, "--resume-from", str(out)), env={}) == 2  # none left


def test_resume_from_budget_includes_prior_spend(repo, ledger, tmp_path, capsys):
    out, _ = _first_run(repo, ledger, tmp_path)
    argv = _argv(repo, ledger, "--resume-from", str(out), budget="0.2")  # < 3 prior + 1 retry
    assert live_eval.main(argv, env={}) == 2
    assert "exceeds the cap" in capsys.readouterr().err


def test_resume_from_refuses_bad_use(repo, ledger, tmp_path):
    out, _ = _first_run(repo, ledger, tmp_path)
    missing = ["--resume-from", str(tmp_path / "nope")]
    assert live_eval.main(_argv(repo, ledger, *missing), env={}) == 2
    assert live_eval.main(_argv(repo, ledger, "--resume-from", str(out), "--pilot-jobs", "3")) == 2
    elsewhere = ["--resume-from", str(out), "--out", str(tmp_path / "other")]
    assert live_eval.main(_argv(repo, ledger, *elsewhere), env={}) == 2
    assert live_eval.main(_argv(repo, ledger), env={}) == 2  # neither --out nor --resume-from
