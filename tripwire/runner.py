"""Benchmark runner (M5): the matrix, a hard budget cap, a committed protocol, resumable receipts.

Adopted from sibling repos, by citation (see docs/PREREGISTRATION.md):

* crucible: ``assert_protocol_committed`` (refuse to run on an uncommitted protocol) and the
  receipt layout ``meta.json`` / ``receipt.jsonl`` / ``result.json``.
* quorum-cal: per-call served-model provenance (``RunRecord.served_models``), a cache-and-resume
  runner (finished run ids in ``receipt.jsonl`` are skipped), a quota-aware halt, and an
  exit-code contract. The numeric codes below are PROPOSED here, not copied.
* sabot: exclusion codes (``harness.Exclusion``).

No live calls: the client is an argument and the default test client is scripted.
"""

from __future__ import annotations

import hashlib
import json
import random
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any

from tripwire.faults import Clock
from tripwire.harness import Exclusion, run_one
from tripwire.prompts import Condition
from tripwire.tasks import PlannedRun, Task, fault_plan
from tripwire.xai import ExecPath, ModelConfig, Pricing, QuotaExhausted, XaiClient

# Design parameters, all owner-decided or OWNER-APPROVED (2026-10-11, docs/PREREGISTRATION.md):
# 7 cells per task = 6 single-fault + clean (intent 1/7; the realized clean share is lower, see
# the preregistration); 3 repeats on a fixed-seed 20% subset. The two seeds are arbitrary.
RUNS_PER_TASK = 7
CLEAN_SHARE = 1 / 7
REPEATS = 3
SUBSET_SHARE = 0.2
PLAN_SEED = 1
SUBSET_SEED = 5
CONDITIONS = (Condition.BASELINE, Condition.PROMPT_FIX, Condition.GATE)
EST_INPUT_TOKENS = 20_000  # OWNER-APPROVED planning estimate per run, for the budget check only
EST_OUTPUT_TOKENS = 2_000


class RunnerRefused(RuntimeError):
    """The runner will not start (uncommitted protocol, unresolved config, budget, no gate)."""


class ExitCode(IntEnum):
    OK = 0
    RUN_ERRORS = 1  # finished, but some runs ended RUN_ERROR
    REFUSED = 2  # refused to start; nothing ran
    BUDGET_HALT = 3  # stopped because the next run could exceed the cap; resumable
    QUOTA_HALT = 4  # stopped because the provider reported quota exhausted; resumable


@dataclass(frozen=True)
class Job:
    plan: PlannedRun
    model: ModelConfig
    path: ExecPath
    condition: Condition
    repeat: int

    @property
    def job_id(self) -> str:
        m, p, c = self.model.label, self.path.value, self.condition.value
        return f"{self.plan.run_id}.{m}.{p}.{c}.k{self.repeat}"


def repeat_subset(tasks: Sequence[Task]) -> frozenset[str]:
    """The fixed-seed 20% of tasks that get repeats and the second execution path."""
    ids = sorted(t.task_id for t in tasks)
    return frozenset(random.Random(SUBSET_SEED).sample(ids, round(SUBSET_SHARE * len(ids))))


def plan_matrix(tasks: Sequence[Task], models: Sequence[ModelConfig]) -> tuple[Job, ...]:
    """Every job: client-side for all tasks; server-side and ``REPEATS`` repeats on the subset."""
    subset = repeat_subset(tasks)
    jobs = []
    for plan in fault_plan(tasks, PLAN_SEED, RUNS_PER_TASK, CLEAN_SHARE):
        extra = plan.task_id in subset
        paths = (ExecPath.CLIENT_SIDE, ExecPath.SERVER_SIDE) if extra else (ExecPath.CLIENT_SIDE,)
        for model in models:
            for path in paths:
                for cond in CONDITIONS:
                    jobs += [
                        Job(plan, model, path, cond, k + 1) for k in range(REPEATS if extra else 1)
                    ]
    return tuple(jobs)


def estimate_run_cost(pricing: Pricing) -> float:
    return pricing.cost(EST_INPUT_TOKENS, EST_OUTPUT_TOKENS)


def assert_protocol_committed(protocol: Path, repo: Path) -> tuple[str, str]:
    """Return (git commit, sha256 of the file); raise unless the file is tracked and unmodified."""

    def git(*args: str) -> str:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        if done.returncode != 0:
            raise RunnerRefused(f"protocol not committed: git {args[0]} failed")
        return done.stdout

    rel = str(protocol.resolve().relative_to(repo.resolve()))
    git("ls-files", "--error-unmatch", "--", rel)
    if git("status", "--porcelain", "--", rel).strip():
        raise RunnerRefused(f"protocol not committed: {rel} has uncommitted changes")
    return git("rev-parse", "HEAD").strip(), hashlib.sha256(protocol.read_bytes()).hexdigest()


def run_matrix(
    jobs: Sequence[Job],
    tasks: Mapping[str, Task],
    client: XaiClient,
    *,
    out_dir: Path,
    protocol: Path,
    repo: Path,
    cap_usd: float,
    pricing: Mapping[str, Pricing | None],
    clock: Clock,
) -> ExitCode:
    """Run ``jobs`` (resuming any already in the receipt); returns the exit code."""
    commit, digest = assert_protocol_committed(protocol, repo)
    meta_path, receipt = out_dir / "meta.json", out_dir / "receipt.jsonl"
    if meta_path.exists() and json.loads(meta_path.read_text())["protocol_sha256"] != digest:
        raise RunnerRefused("receipt directory belongs to a different protocol version")
    done, spent = _read_receipt(receipt)
    _preflight([j for j in jobs if j.job_id not in done], pricing, cap_usd, spent)
    out_dir.mkdir(parents=True, exist_ok=True)
    now = clock().isoformat()
    session = {"started": now, "git_commit": commit, "cap_usd": cap_usd, "jobs": len(jobs)}
    first = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta_path.write_text(
        json.dumps(
            {
                "protocol": str(protocol),
                "protocol_sha256": digest,
                "git_commit": first.get("git_commit", commit),  # the first session's, kept
                "cap_usd": cap_usd,
                "jobs": len(jobs),
                "started": first.get("started", now),
                "sessions": [*first.get("sessions", []), session],
            },
            sort_keys=True,
        )
    )
    (out_dir / "ledgers").mkdir(exist_ok=True)
    code, ran = ExitCode.OK, 0
    for job in jobs:
        if job.job_id in done:
            continue
        price = pricing[job.model.label]
        assert price is not None  # _preflight guarantees it
        if spent + estimate_run_cost(price) > cap_usd:
            code = ExitCode.BUDGET_HALT
            break
        _supersede_ledger(out_dir / "ledgers", job.job_id)
        try:
            rec = run_one(
                job.plan,
                tasks[job.plan.task_id],
                job.model,
                job.path,
                job.condition,
                client,
                run_id=job.job_id,
                clock=clock,
                pricing=price,
                ledger_path=out_dir / "ledgers" / f"{job.job_id}.jsonl",
            )
        except QuotaExhausted:
            code = ExitCode.QUOTA_HALT
            break
        with receipt.open("a") as fh:
            fh.write(rec.to_json() + "\n")
        spent += rec.cost_usd or 0.0
        ran += 1
        if rec.exclusion == Exclusion.RUN_ERROR.value:
            code = ExitCode.RUN_ERRORS if code is ExitCode.OK else code
    (out_dir / "result.json").write_text(
        json.dumps(
            {
                "exit_code": int(code),
                "exit_name": code.name,
                "ran_this_session": ran,
                "records_total": len(_read_receipt(receipt)[0]),
                "spent_usd": spent,
                "cap_usd": cap_usd,
            },
            sort_keys=True,
        )
    )
    return code


def _supersede_ledger(ledgers: Path, job_id: str) -> None:
    """Move a ledger left by an unfinished run (and its ``.head``) aside; never delete it."""
    stale = [ledgers / f"{job_id}.jsonl", ledgers / f"{job_id}.jsonl.head"]
    if not any(p.exists() for p in stale):
        return
    parked = ledgers / "superseded"
    parked.mkdir(exist_ok=True)
    n = 1
    while any((parked / p.name.replace(job_id, f"{job_id}.{n}", 1)).exists() for p in stale):
        n += 1
    for p in stale:
        if p.exists():
            p.rename(parked / p.name.replace(job_id, f"{job_id}.{n}", 1))


def _preflight(
    jobs: Sequence[Job], pricing: Mapping[str, Pricing | None], cap_usd: float, spent: float
) -> None:
    if not cap_usd > 0:
        raise RunnerRefused("a positive budget cap is required")
    total = spent
    for job in jobs:
        price = pricing.get(job.model.label)
        if not job.model.resolved or price is None:
            raise RunnerRefused(f"model or pricing UNRESOLVED for {job.model.label!r}")
        if job.condition is Condition.GATE:
            raise RunnerRefused("gate condition requested but the gate (M6) does not exist")
        total += estimate_run_cost(price)
    if total > cap_usd:
        raise RunnerRefused(f"projected spend {total:.2f} USD exceeds the cap {cap_usd:.2f} USD")


def _read_receipt(path: Path) -> tuple[set[str], float]:
    if not path.exists():
        return set(), 0.0
    rows: list[dict[str, Any]] = [json.loads(x) for x in path.read_text().splitlines() if x]
    return {r["run_id"] for r in rows}, sum(r["cost_usd"] or 0.0 for r in rows)
