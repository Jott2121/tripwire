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
import math
import os
import random
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any

from tripwire.faults import Clock
from tripwire.harness import Exclusion, RunRecord, run_one
from tripwire.prompts import Condition
from tripwire.tasks import PlannedRun, Task, fault_plan
from tripwire.xai import ExecPath, ModelConfig, Pricing, QuotaExhausted, XaiClient

# Lean design for the $25 first full run (issue #25; every change from the M5 design is logged as
# a deviation in docs/PREREGISTRATION.md). 6 cells per task slot-wise = 5 faults drawn from the
# task's admissible list plus 1 clean (OWNER-APPROVED 2026-10-11: M5's 7 and 1/7 would cost 560
# runs for two conditions, over the 480 the owner's $25 buys). No repeats and no server-side runs
# for now (remote MCP deferral OWNER-APPROVED 2026-10-11): ``SERVER_SUBSET_SHARE`` is 0 until a
# tunnel exists. The two seeds are arbitrary.
RUNS_PER_TASK = 6
CLEAN_SHARE = 1 / 6
REPEATS = 1
SERVER_SUBSET_SHARE = 0.0
PLAN_SEED = 1
SUBSET_SEED = 5
CONDITIONS = (Condition.BASELINE, Condition.PROMPT_FIX)  # the gate is its own session (M6)
EST_INPUT_TOKENS = 20_000  # OWNER-APPROVED planning estimate per run, for the budget check only
EST_OUTPUT_TOKENS = 2_000
HARD_CEILING_USD = 50.0  # OWNER-DECIDED (issue #25): cumulative across all sessions; lower only
FIRST_RUN_BUDGET_USD = 25.0  # OWNER-DECIDED: the lean first full run


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


def server_subset(tasks: Sequence[Task], share: float = SERVER_SUBSET_SHARE) -> frozenset[str]:
    """The fixed-seed share of tasks that get the server-side path and repeats (none by default)."""
    ids = sorted(t.task_id for t in tasks)
    return frozenset(random.Random(SUBSET_SEED).sample(ids, round(share * len(ids))))


def plan_matrix(
    tasks: Sequence[Task],
    models: Sequence[ModelConfig],
    conditions: Sequence[Condition] = CONDITIONS,
    server_share: float = SERVER_SUBSET_SHARE,
) -> tuple[Job, ...]:
    """Every job: client-side for all tasks; server-side and ``REPEATS`` repeats on the subset."""
    subset = server_subset(tasks, server_share)
    jobs = []
    for plan in fault_plan(tasks, PLAN_SEED, RUNS_PER_TASK, CLEAN_SHARE):
        extra = plan.task_id in subset
        paths = (ExecPath.CLIENT_SIDE, ExecPath.SERVER_SIDE) if extra else (ExecPath.CLIENT_SIDE,)
        for model in models:
            for path in paths:
                for cond in conditions:
                    if cond is Condition.GATE and path is ExecPath.SERVER_SIDE:
                        continue  # not gateable: remote MCP results never reach the harness
                    jobs += [
                        Job(plan, model, path, cond, k + 1) for k in range(REPEATS if extra else 1)
                    ]
    return tuple(jobs)


def estimate_run_cost(pricing: Pricing) -> float:
    return pricing.cost(EST_INPUT_TOKENS, EST_OUTPUT_TOKENS)


def wilson_interval(successes: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """Wilson score 95% interval for ``successes`` of ``n`` independent runs.

    Used for the preregistration's planned-precision table. It treats runs as independent, so it
    is narrower than the replicate-cluster bootstrap that the analysis reports.
    """
    if n < 1 or not 0 <= successes <= n:
        raise ValueError(f"need 0 <= successes <= n and n >= 1, got {successes}/{n}")
    p, d = successes / n, 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


class SpendLedgerError(RunnerRefused):
    """The cumulative spend ledger is missing or corrupt: fail closed, never treat it as zero."""


def init_spend_ledger(path: Path, clock: Clock) -> None:
    """Create the shared spend ledger once. Refuses to touch one that exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {"kind": "header", "version": 1, "created": clock().isoformat()}
    with path.open("x") as fh:  # FileExistsError if present: an existing ledger is never reset
        fh.write(json.dumps(header, sort_keys=True) + "\n")


def read_spend(path: Path) -> float:
    """Total USD in the shared ledger. Missing, unreadable or malformed => SpendLedgerError."""
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise SpendLedgerError(f"spend ledger unreadable ({type(exc).__name__}): {path}") from exc
    try:
        header = json.loads(lines[0]) if lines else None
        if not isinstance(header, dict) or header.get("kind") != "header":
            raise ValueError("first line is not a header")
        total = 0.0
        for line in lines[1:]:
            row = json.loads(line)
            cost = row["cost_usd"]
            if row["kind"] != "spend" or isinstance(cost, bool):
                raise ValueError("not a spend row")
            if not isinstance(cost, int | float):
                raise ValueError("cost is not a number")
            if not math.isfinite(cost) or cost < 0:
                raise ValueError("cost is not a finite non-negative number")
            total += float(cost)
    except (ValueError, KeyError, TypeError) as exc:
        raise SpendLedgerError(f"spend ledger corrupt ({exc}): {path}") from exc
    return total


def _append_spend(path: Path, rec: RunRecord, out_dir: Path, fallback_usd: float) -> None:
    """Append one run's cost (ticks-based when the provider sent it) and flush it to disk."""
    row = {
        "kind": "spend",
        "run_id": rec.run_id,
        "out_dir": str(out_dir),
        "cost_usd": rec.cost_usd if rec.cost_usd is not None else fallback_usd,
        "cost_in_usd_ticks": rec.cost_in_usd_ticks,
        "timestamp": rec.timestamp,
    }
    with path.open("a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


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
    spend_ledger: Path,
    ceiling_usd: float = HARD_CEILING_USD,
) -> ExitCode:
    """Run ``jobs`` (resuming any already in the receipt); returns the exit code.

    ``cap_usd`` bounds this output directory. ``spend_ledger`` is the one shared file that
    records every run's cost across all sessions and directories; ``ceiling_usd`` bounds it and
    can only be lowered from ``HARD_CEILING_USD``. A missing or corrupt ledger refuses the run.
    """
    if not 0 < ceiling_usd <= HARD_CEILING_USD:
        raise RunnerRefused(f"ceiling must be in (0, {HARD_CEILING_USD}] USD, got {ceiling_usd}")
    commit, digest = assert_protocol_committed(protocol, repo)
    ledger_spent = read_spend(spend_ledger)
    meta_path, receipt = out_dir / "meta.json", out_dir / "receipt.jsonl"
    if meta_path.exists() and json.loads(meta_path.read_text())["protocol_sha256"] != digest:
        raise RunnerRefused("receipt directory belongs to a different protocol version")
    done, spent = _read_receipt(receipt)
    pending = [j for j in jobs if j.job_id not in done]
    _preflight(pending, pricing, cap_usd, spent, ledger_spent, ceiling_usd)
    out_dir.mkdir(parents=True, exist_ok=True)
    now = clock().isoformat()
    session = {
        "started": now,
        "git_commit": commit,
        "cap_usd": cap_usd,
        "ceiling_usd": ceiling_usd,
        "spend_ledger": str(spend_ledger),
        "jobs": len(jobs),
    }
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
        ledger_spent = read_spend(spend_ledger)  # re-read: other sessions may have spent since
        est = estimate_run_cost(price)
        if spent + est > cap_usd or ledger_spent + est > ceiling_usd:
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
        _append_spend(spend_ledger, rec, out_dir, est)  # first: an over-count beats an under-count
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
                "ceiling_usd": ceiling_usd,
                "ledger_spent_usd": read_spend(spend_ledger),
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
    jobs: Sequence[Job],
    pricing: Mapping[str, Pricing | None],
    cap_usd: float,
    spent: float,
    ledger_spent: float,
    ceiling_usd: float,
) -> None:
    if not cap_usd > 0:
        raise RunnerRefused("a positive budget cap is required")
    total = spent
    projected = 0.0
    for job in jobs:
        price = pricing.get(job.model.label)
        if not job.model.resolved or price is None:
            raise RunnerRefused(f"model or pricing UNRESOLVED for {job.model.label!r}")
        if job.condition is Condition.GATE and job.path is ExecPath.SERVER_SIDE:
            raise RunnerRefused("the gate cannot run on the server-side path")
        total += estimate_run_cost(price)
        projected += estimate_run_cost(price)
    if ledger_spent + projected > ceiling_usd:
        raise RunnerRefused(
            f"spent {ledger_spent:.2f} + projected {projected:.2f} USD exceeds the "
            f"cumulative ceiling {ceiling_usd:.2f} USD"
        )
    if total > cap_usd:
        raise RunnerRefused(f"projected spend {total:.2f} USD exceeds the cap {cap_usd:.2f} USD")


def _read_receipt(path: Path) -> tuple[set[str], float]:
    if not path.exists():
        return set(), 0.0
    rows: list[dict[str, Any]] = [json.loads(x) for x in path.read_text().splitlines() if x]
    return {r["run_id"] for r in rows}, sum(r["cost_usd"] or 0.0 for r in rows)
