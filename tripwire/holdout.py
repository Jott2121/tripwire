"""Holdout confirmation plan ``holdout-1`` (amendment A4): fresh fault draws, all three conditions.

    python -m tripwire.holdout --out runs/targets/holdout-1.json   # author (reads the ledger once)
    python -m tripwire.holdout --check runs/targets/holdout-1.json # verify (no ledger read)

Fresh jobs: the committed task set is re-planned with ``HOLDOUT_PLAN_SEED`` (never used before; the
benchmark plan uses ``runner.PLAN_SEED``), which draws new fault assignments and new per-run fault
seeds. Run ids carry the ``H1-`` prefix, so no holdout job id can equal any id already in the spend
ledger, and authoring refuses if any (task, fault, fault_seed) triple matches the benchmark plan.
From that plan ``SLOTS_PER_FAULT`` slots per fault F1-F6 and ``CLEAN_SLOTS`` clean slots are taken
in a seeded hash order (outcome-blind), and each slot runs under baseline, prompt_fix and gate.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from tripwire import runner, tasks, xai
from tripwire.faults import FaultKind
from tripwire.prompts import Condition
from tripwire.targets import TargetError
from tripwire.xai import ExecPath

NAME = "holdout-1"
HOLDOUT_PLAN_SEED = 20261013  # OWNER-APPROVED in A4, fixed before any holdout run
PREFIX = "H1-"
SLOTS_PER_FAULT = 3
CLEAN_SLOTS = 6
CONDITIONS = (Condition.BASELINE, Condition.PROMPT_FIX, Condition.GATE)
BUDGET_USD_CAP = 6.0  # OWNER-APPROVED hard cap for this run (A4)
REAL_USD_PER_RUN = 0.075  # observed mean per gated run, gate-confirm-1 (A4 planning figure)
# Pass/fail criteria, FIXED IN ADVANCE by amendment A4 (strict floor = excluded runs count as
# incorrect). PASS needs every criterion; a run with fewer than all jobs recorded is INCONCLUSIVE.
P1_MIN_STRICT_GAIN_PTS = 10.0  # gate ALL-faults strict >= prompt_fix strict + 10 points
P2_MIN_GATE_PUBLISHED = 0.90  # gate ALL-faults published >= 90%
P3_MAX_GATE_CLEAN_FF = 1 / 6  # gate clean false-flag strict <= 1 of 6, and <= prompt_fix's
P4_MAX_COST_RATIO = 1.25  # gate $/correct (published) <= 1.25 x prompt_fix's
DEFAULT_PATH = Path("runs") / "targets" / f"{NAME}.json"
_LEDGER = Path("runs") / "spend_ledger.jsonl"


def _rank(run_id: str) -> str:
    return hashlib.sha256(f"{NAME}:{HOLDOUT_PLAN_SEED}:{run_id}".encode()).hexdigest()


def holdout_runs() -> tuple[tasks.PlannedRun, ...]:
    """The fresh plan, renamed with the holdout prefix."""
    plan = tasks.fault_plan(
        tasks.default_tasks(), HOLDOUT_PLAN_SEED, runner.RUNS_PER_TASK, runner.CLEAN_SHARE
    )
    return tuple(dataclasses.replace(p, run_id=PREFIX + p.run_id) for p in plan)


def select_slots(runs: Sequence[tasks.PlannedRun]) -> tuple[tasks.PlannedRun, ...]:
    """Per fault (and clean) the first slots in hash order, at most one per task per stratum."""
    picked: list[tasks.PlannedRun] = []
    strata: list[tuple[FaultKind | None, int]] = [(f, SLOTS_PER_FAULT) for f in FaultKind]
    strata.append((None, CLEAN_SLOTS))
    for fault, n in strata:
        seen: set[str] = set()
        pool = sorted((r for r in runs if r.fault == fault), key=lambda r: _rank(r.run_id))
        chosen = []
        for r in pool:
            if r.task_id not in seen:
                chosen.append(r)
                seen.add(r.task_id)
            if len(chosen) == n:
                break
        if len(chosen) < n:
            raise TargetError(f"only {len(chosen)} distinct-task slots for {fault or 'clean'}")
        picked += chosen
    return tuple(sorted(picked, key=lambda r: r.run_id))


def jobs(slots: Sequence[tasks.PlannedRun] | None = None) -> tuple[runner.Job, ...]:
    slots = select_slots(holdout_runs()) if slots is None else slots
    return tuple(
        runner.Job(p, m, ExecPath.CLIENT_SIDE, c, 1)
        for p in slots
        for m in xai.MODEL_CONFIGS
        for c in CONDITIONS
    )


def _benchmark_triples() -> set[tuple[str, Any, int]]:
    plan = tasks.fault_plan(
        tasks.default_tasks(), runner.PLAN_SEED, runner.RUNS_PER_TASK, runner.CLEAN_SHARE
    )
    return {(p.task_id, p.fault, p.fault_seed) for p in plan}


def _all_ledger_run_ids(ledger: Path) -> set[str]:
    rows = [json.loads(x) for x in ledger.read_text().splitlines()[1:] if x]
    return {r["run_id"] for r in rows if isinstance(r.get("run_id"), str)}


def build_document(ledger: Path = _LEDGER) -> dict[str, Any]:
    js = jobs()
    ids = [j.job_id for j in js]
    clash = set(ids) & _all_ledger_run_ids(ledger)
    if clash:
        raise TargetError(f"{len(clash)} holdout job id(s) already in the spend ledger")
    slots = sorted({j.plan for j in js}, key=lambda p: p.run_id)
    reused = [p for p in slots if (p.task_id, p.fault, p.fault_seed) in _benchmark_triples()]
    if reused:
        raise TargetError(f"{len(reused)} holdout slot(s) reuse a benchmark fault draw")
    est = round(len(js) * REAL_USD_PER_RUN, 4)
    if est > BUDGET_USD_CAP:
        raise TargetError(f"estimate {est:.2f} USD exceeds the {BUDGET_USD_CAP:.2f} USD cap")
    return {
        "name": NAME,
        "amendment": "A4",
        "plan_seed": HOLDOUT_PLAN_SEED,
        "conditions": [c.value for c in CONDITIONS],
        "budget_usd_cap": BUDGET_USD_CAP,
        "est_cost_usd": est,
        "est_usd_per_run": REAL_USD_PER_RUN,
        "counts": {"slots": len(slots), "jobs": len(js)},
        "slots": [
            {
                "run_id": p.run_id,
                "task_id": p.task_id,
                "fault": p.fault.value if p.fault else None,
                "fault_seed": p.fault_seed,
            }
            for p in slots
        ],  # fmt: skip
        "job_ids": ids,
    }


def verify_document(doc: dict[str, Any]) -> tuple[str, ...]:
    """Fail closed unless ``doc`` equals regeneration (ledger-independent part) and fits the cap."""
    want = [j.job_id for j in jobs()]
    if doc.get("name") != NAME or doc.get("plan_seed") != HOLDOUT_PLAN_SEED:
        raise TargetError("not the holdout-1 document")
    if doc.get("job_ids") != want:
        raise TargetError("job_ids differ from regeneration (stale holdout file)")
    if not 0 < float(doc.get("budget_usd_cap", 0)) <= BUDGET_USD_CAP:
        raise TargetError("budget cap missing or above the A4 cap")
    return tuple(want)


def _cell(rep: dict[str, Any], cond: str, fault: str) -> dict[str, Any]:
    for c in rep["cells"]:
        if c["condition"] == cond and c["fault"] == fault:
            return c
    raise TargetError(f"report has no {cond}/{fault} cell")


def evaluate(rep: dict[str, Any], expected_jobs: int | None = None) -> dict[str, Any]:
    """Apply the A4 criteria to a ``tripwire.report.analyse`` result. Never relaxes a threshold."""
    expected = len(jobs()) if expected_jobs is None else expected_jobs
    g, pf = _cell(rep, "gate", "ALL"), _cell(rep, "prompt_fix", "ALL")
    gc, pc = _cell(rep, "gate", "clean"), _cell(rep, "prompt_fix", "clean")
    g_cost = g["published_cost_per_correct"]["value"]
    pf_cost = pf["published_cost_per_correct"]["value"]
    g_pub = g["published"]["value"]
    eps = 1e-9  # thresholds are inclusive; absorb float rounding only
    checks = {
        "P1_strict_gain": (g["strict"]["value"] - pf["strict"]["value"]) * 100
        >= P1_MIN_STRICT_GAIN_PTS - eps,
        "P2_gate_published": g_pub is not None and g_pub >= P2_MIN_GATE_PUBLISHED - eps,
        "P3_clean_false_flags": gc["strict"]["value"] <= P3_MAX_GATE_CLEAN_FF + eps
        and gc["strict"]["value"] <= pc["strict"]["value"] + eps,
        "P4_cost_ratio": g_cost is not None
        and pf_cost is not None
        and g_cost <= P4_MAX_COST_RATIO * pf_cost + eps,
    }
    complete = rep["records"] >= expected and not rep["incomplete"]
    verdict = ("PASS" if all(checks.values()) else "FAIL") if complete else "INCONCLUSIVE"
    return {"verdict": verdict, "complete": complete, "checks": checks}


def render_document() -> str:
    return json.dumps(build_document(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--out", type=Path)
    g.add_argument("--check", type=Path)
    a = ap.parse_args(argv)
    if a.check:
        try:
            verify_document(json.loads(a.check.read_text()))
        except (TargetError, OSError, ValueError) as exc:
            print(f"invalid: {a.check}: {exc}", file=sys.stderr)
            return 1
        print(f"ok: {a.check} is the holdout-1 plan")
        return 0
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(render_document(), encoding="utf-8")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
