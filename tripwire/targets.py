"""Targeted live-confirmation job sets (issue #46): a committed, deterministic job list.

    python -m tripwire.targets --out runs/targets/gate-confirm-1.json     # author (reads ledger)
    python -m tripwire.targets --check runs/targets/gate-confirm-1.json    # verify (no ledger)

``gate-confirm-1`` is the confirmation run for the #45 gate (store cross-check for F1/F3 and the
clean-refusal downgrade, see ``docs/amendments/A1-gate-store-crosscheck.md``). It is built by rule,
never from cherry-picked task ids:

* **skipped** -- the ``Condition.GATE`` jobs that run ``38285916667`` did not reach. That run hit
  ``BUDGET_HALT`` after 186 of the 240 planned gate jobs; the 54 it skipped are exactly the gate
  plan (:func:`tripwire.runner.plan_matrix`) minus the gate job ids already recorded in the
  committed spend ledger. Which 186 ran is a function of live cost, so it is read once from the
  committed ledger at authoring time and then FROZEN into this file; verification never re-reads
  the (mutable) ledger.
* **topup** -- a deterministic stratified sample of gate jobs whose planted fault is F1, F3, or
  clean (the strata the #45 change touches), drawn with :func:`tripwire.runner.pilot_subset` so no
  fault is favoured, excluding anything already in *skipped*, enough to reach :data:`TARGET_TOTAL`.

The whole set is bounded: its planning estimate must fit :data:`BUDGET_USD_CAP`. Authoring must run
against the pre-dispatch ledger; once the confirmation run spends, the ledger gains these rows, so
re-authoring would change *skipped* -- hence the freeze. Pure except :func:`main` and the one
ledger read in :func:`author_gate_confirm_1`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tripwire import runner, tasks, xai
from tripwire.prompts import Condition

NAME = "gate-confirm-1"
CONDITION_SET = "gate"
SOURCE_RUN = "38285916667"
TARGET_TOTAL = 60
BUDGET_USD_CAP = 4.0  # OWNER-APPROVED (issue #46) for this one confirmation run
TOPUP_FAULTS = ("F1", "F3", "clean")  # the strata the #45 gate change touches
DEFAULT_PATH = Path("runs") / "targets" / f"{NAME}.json"
_LEDGER = Path("runs") / "spend_ledger.jsonl"


class TargetError(runner.RunnerRefused):
    """A target set cannot be built or verified (fail closed: never dispatch a guessed list).

    A ``RunnerRefused`` so the live-eval driver refuses on it like any other fail-closed check.
    """


def gate_plan() -> tuple[runner.Job, ...]:
    """The full ``Condition.GATE`` job matrix, in plan order (240 jobs for the committed set)."""
    return runner.plan_matrix(tasks.default_tasks(), xai.MODEL_CONFIGS, (Condition.GATE,))


def _fault_label(job: runner.Job) -> str:
    return job.plan.fault.value if job.plan.fault else "clean"


HOLDOUT_PREFIX = "H1-"  # mirrors tripwire.holdout.PREFIX (holdout imports this module)


def ran_gate_job_ids(ledger: Path) -> frozenset[str]:
    """Gate job ids already recorded in the committed spend ledger (the jobs that were paid for).

    The ledger is read once, here, at authoring time only. A malformed line fails closed.
    """
    try:
        lines = ledger.read_text().splitlines()
    except OSError as exc:
        raise TargetError(f"spend ledger unreadable ({type(exc).__name__}): {ledger}") from exc
    ids: set[str] = set()
    for line in lines[1:]:
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise TargetError(f"spend ledger corrupt ({exc}): {ledger}") from exc
        run_id = row.get("run_id")
        # holdout jobs (amendment A4, "H1-" prefix) are a separate set, never gate-confirm-1 jobs
        is_gate = (
            isinstance(run_id, str)
            and f".{CONDITION_SET}." in run_id
            and not run_id.startswith(HOLDOUT_PREFIX)
        )
        if row.get("kind") == "spend" and is_gate:
            ids.add(run_id)
    return frozenset(ids)


def estimate_jobs_usd(jobs: Sequence[runner.Job]) -> float:
    """Planning-estimate cost of ``jobs``. Fails closed on a job whose model has no price, so a
    budget check can never under-count by silently dropping an unpriced job."""
    total = 0.0
    for job in jobs:
        price = xai.PRICING.get(job.model.label)
        if price is None:
            raise TargetError(f"no price for model {job.model.label!r}: cannot estimate cost")
        total += runner.estimate_run_cost(price)
    return total


def _estimate_usd(job_ids: Sequence[str], plan: Sequence[runner.Job]) -> float:
    by_id = {j.job_id: j for j in plan}
    return round(estimate_jobs_usd([by_id[jid] for jid in job_ids]), 6)


def build_document(
    plan: Sequence[runner.Job], ran: frozenset[str], target_total: int = TARGET_TOTAL
) -> dict[str, Any]:
    """Assemble the committed target document from the plan and the already-run gate job ids."""
    plan_ids = [j.job_id for j in plan]
    unknown = ran - set(plan_ids)
    if unknown:
        raise TargetError(f"{len(unknown)} already-run gate id(s) are not in the current plan")
    skipped = [jid for jid in plan_ids if jid not in ran]
    if not skipped:
        raise TargetError("no skipped gate jobs: nothing to confirm")
    need = target_total - len(skipped)
    if need < 0:
        raise TargetError(f"{len(skipped)} skipped already exceed target {target_total}")
    topup_ids: list[str] = []
    if need:
        skipped_set = set(skipped)
        pool = tuple(
            j for j in plan if j.job_id not in skipped_set and _fault_label(j) in TOPUP_FAULTS
        )
        if len(pool) < need:
            raise TargetError(f"only {len(pool)} F1/F3/clean jobs left to top up {need}")
        topup_ids = [j.job_id for j in runner.pilot_subset(pool, need)]
    chosen = set(skipped) | set(topup_ids)
    job_ids = [jid for jid in plan_ids if jid in chosen]  # plan order, deduped
    est = _estimate_usd(job_ids, plan)
    if est > BUDGET_USD_CAP:
        raise TargetError(f"estimate {est:.2f} USD exceeds the {BUDGET_USD_CAP:.2f} USD cap")
    return {
        "name": NAME,
        "issue": 46,
        "condition_set": CONDITION_SET,
        "source_run": SOURCE_RUN,
        "budget_usd_cap": BUDGET_USD_CAP,
        "est_cost_usd": est,
        "selection": (
            "gate jobs run 38285916667 skipped at BUDGET_HALT (plan minus already-run gate "
            "ids in the committed ledger), topped up to a stratified F1/F3/clean sample via "
            "runner.pilot_subset; frozen from the committed ledger at authoring time"
        ),
        "counts": {
            "plan": len(plan_ids),
            "already_run": len(ran),
            "skipped": len(skipped),
            "topup": len(topup_ids),
            "total": len(job_ids),
        },
        "skipped_job_ids": skipped,
        "topup_job_ids": sorted(topup_ids),
        "job_ids": job_ids,
    }


def author_gate_confirm_1(ledger: Path = _LEDGER) -> dict[str, Any]:
    """Author the document by reading the committed ledger once (the only ledger read)."""
    plan = gate_plan()
    return build_document(plan, ran_gate_job_ids(ledger))


def verify_document(doc: Mapping[str, Any], plan: Sequence[runner.Job] | None = None) -> None:
    """Fail closed unless the committed document is a self-consistent gate target set.

    Ledger-independent on purpose: it checks the committed job ids against the plan and the frozen
    skipped/topup split, so a growing ledger never breaks the check. Raises :class:`TargetError`.
    """
    plan = gate_plan() if plan is None else plan
    by_id = {j.job_id: j for j in plan}
    if doc.get("condition_set") != CONDITION_SET:
        raise TargetError(f"condition_set must be {CONDITION_SET!r}")
    skipped = list(doc.get("skipped_job_ids", []))
    topup = list(doc.get("topup_job_ids", []))
    job_ids = list(doc.get("job_ids", []))
    if not job_ids:
        raise TargetError("job_ids is empty")
    if len(set(job_ids)) != len(job_ids):
        raise TargetError("job_ids has duplicates")
    missing = [jid for jid in job_ids if jid not in by_id]
    if missing:
        raise TargetError(f"{len(missing)} job id(s) are not in the gate plan (stale targets)")
    if set(skipped) & set(topup):
        raise TargetError("skipped and topup overlap")
    if set(skipped) | set(topup) != set(job_ids):
        raise TargetError("job_ids is not exactly skipped + topup")
    bad = [jid for jid in topup if _fault_label(by_id[jid]) not in TOPUP_FAULTS]
    if bad:
        raise TargetError(f"{len(bad)} topup job(s) are not F1/F3/clean")
    counts = doc.get("counts") or {}
    if counts and (
        counts.get("total") != len(job_ids)
        or counts.get("skipped") != len(skipped)
        or counts.get("topup") != len(topup)
    ):
        raise TargetError("counts block disagrees with the job id lists")
    est = _estimate_usd(job_ids, plan)
    if est > BUDGET_USD_CAP:
        raise TargetError(f"estimate {est:.2f} USD exceeds the {BUDGET_USD_CAP:.2f} USD cap")


def load_targets(path: Path, condition_set: str, plan: Sequence[runner.Job]) -> tuple[str, ...]:
    """Load and verify a committed target file for ``condition_set``; return its job ids.

    The driver calls this: it fails closed on a mismatched condition set, a bad document, or a job
    id that is not in the current plan (a stale target set must never silently run fewer jobs).
    """
    try:
        doc = json.loads(path.read_text())
    except ValueError as exc:
        raise TargetError(f"target file is not valid JSON: {path} ({exc})") from exc
    if doc.get("condition_set") != condition_set:
        raise TargetError(
            f"targets are for {doc.get('condition_set')!r}, not condition-set {condition_set!r}"
        )
    verify_document(doc, plan)
    return tuple(doc["job_ids"])


def render_document() -> str:
    return json.dumps(author_gate_confirm_1(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--out", type=Path, help="author the target file here (reads the ledger)")
    group.add_argument("--check", type=Path, help="verify an existing target file (no ledger)")
    args = parser.parse_args(argv)
    if args.check:
        try:
            verify_document(json.loads(args.check.read_text()))
        except (TargetError, OSError, ValueError) as exc:
            print(f"invalid: {args.check}: {exc}", file=sys.stderr)
            return 1
        print(f"ok: {args.check} is a valid gate target set")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_document(), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
