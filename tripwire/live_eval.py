"""Live-eval driver (M7): the one entry point the manual workflow calls.

``python -m tripwire.live_eval --condition-set baseline-prompt-fix --budget-usd 25 --confirm ...``

Fail closed, in order: the confirm string, a readable spend ledger whose row count matches what the
operator expects (a stale copy would under-count spend), a budget no larger than the room left under
``runner.HARD_CEILING_USD``, a committed protocol, and (live mode only) an API key plus a confirmed
wire format. ``--dry-run`` swaps in a scripted client and a throwaway ledger so the whole path runs
with no secret and no network. The key is read from the environment here and nowhere else.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from tripwire import runner, tasks, xai
from tripwire.prompts import Condition

CONFIRM_PHRASE = "I-APPROVE-LIVE-SPEND"
CONDITION_SETS: Mapping[str, tuple[Condition, ...]] = {
    "baseline-prompt-fix": runner.CONDITIONS,  # the lean first run
    "gate": (Condition.GATE,),  # its own session
}
KEY_ENV = "XAI_API_KEY"
DRY_RUN_REPLY = json.dumps(
    {"status": "refused", "value": None, "unit": None, "work_order_id": None, "flags": []}
)


class DryRunClient:
    """Answers every request with one scripted refusal at the planning-estimate token counts."""

    def chat(self, request: xai.ChatRequest) -> xai.ChatResponse:
        return xai.ChatResponse(
            request.model, DRY_RUN_REPLY, runner.EST_INPUT_TOKENS, runner.EST_OUTPUT_TOKENS
        )


def check_budget(budget_usd: float, ledger: Path, expect_rows: int) -> float:
    """Return the room left under the ceiling, or raise. Never creates or repairs the ledger."""
    spent = runner.read_spend(ledger)
    rows = len(ledger.read_text().splitlines()) - 1
    if rows != expect_rows:
        raise runner.RunnerRefused(f"ledger has {rows} spend rows, operator expected {expect_rows}")
    remaining = runner.HARD_CEILING_USD - spent
    if not 0 < budget_usd <= remaining:
        raise runner.RunnerRefused(
            f"budget {budget_usd} USD must be in (0, {remaining:.2f}] (spent {spent:.2f} of "
            f"{runner.HARD_CEILING_USD:.2f})"
        )
    return remaining


def _spend_rows(path: Path) -> list[dict[str, object]]:
    """The spend rows of a ledger that ``read_spend`` accepts (so each has a numeric cost)."""
    runner.read_spend(path)
    rows = [json.loads(line) for line in path.read_text().splitlines()[1:]]
    return [r for r in rows if r["kind"] == "spend"]


def check_prior_ledger(committed: Path, prior: Path) -> None:
    """Refuse unless the committed ledger already contains everything the last paid run recorded.

    Catches a forgotten ledger commit: the last paid run's artifact ledger holds rows the stale
    committed file lacks. Rows match on (run_id, timestamp, cost_usd); the committed total must
    also be at least the prior total.
    """

    def ident(row: dict[str, object]) -> tuple[object, object, object]:
        return row.get("run_id"), row.get("timestamp"), row.get("cost_usd")

    have = {ident(r) for r in _spend_rows(committed)}
    missing = [r for r in _spend_rows(prior) if ident(r) not in have]
    if missing:
        raise runner.RunnerRefused(
            f"committed ledger lacks {len(missing)} spend row(s) from the last paid run "
            "(commit its spend_ledger.jsonl artifact first)"
        )
    if runner.read_spend(committed) < runner.read_spend(prior):
        raise runner.RunnerRefused("committed ledger total is below the last paid run's total")


PILOT_MIN, PILOT_MAX, PILOT_MAX_BUDGET_USD = 10, 20, 2.0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tripwire.live_eval")
    p.add_argument("--condition-set", choices=sorted(CONDITION_SETS), required=True)
    p.add_argument("--budget-usd", type=float, required=True)
    p.add_argument("--confirm", required=True, help=f"must equal {CONFIRM_PHRASE}")
    p.add_argument("--ledger-rows", type=int, required=True, help="spend rows you expect")
    p.add_argument("--spend-ledger", type=Path, default=Path("runs/spend_ledger.jsonl"))
    p.add_argument("--out", type=Path, help="output directory (required unless --resume-from)")
    p.add_argument(
        "--resume-from",
        type=Path,
        metavar="DIR",
        help="a prior run's out/run directory: re-attempt only its RUN_ERROR jobs, in place. "
        "Its recorded spend counts against --budget-usd; the ledger ceiling still applies",
    )
    p.add_argument("--repo", type=Path, default=Path("."))
    p.add_argument("--dry-run", action="store_true", help="scripted client, throwaway ledger")
    p.add_argument("--max-jobs", type=int, help="dry-run only: run the first N jobs")
    p.add_argument(
        "--pilot-jobs",
        type=int,
        help=f"run a deterministic stratified N-job pilot; live needs {PILOT_MIN}<=N<={PILOT_MAX} "
        f"and budget<={PILOT_MAX_BUDGET_USD}",
    )
    prior = p.add_mutually_exclusive_group()
    prior.add_argument("--prior-ledger", type=Path, help="the last paid run's spend ledger")
    prior.add_argument(
        "--no-prior-paid-run",
        action="store_true",
        help="only when the Actions API shows zero prior paid runs",
    )
    return p


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = os.environ if env is None else env
    try:
        return _run(args, env)
    except (runner.RunnerRefused, xai.XaiError, OSError) as exc:
        print(f"REFUSED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return int(runner.ExitCode.REFUSED)


def _run(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    if args.confirm != CONFIRM_PHRASE:
        raise runner.RunnerRefused("confirm string does not match")
    if args.max_jobs is not None and not args.dry_run:
        raise runner.RunnerRefused("--max-jobs is dry-run only")
    pilot: int | None = args.pilot_jobs
    if pilot is not None and args.max_jobs is not None:
        raise runner.RunnerRefused("--pilot-jobs and --max-jobs are exclusive")
    if pilot is not None and not args.dry_run:
        if not PILOT_MIN <= pilot <= PILOT_MAX or args.budget_usd > PILOT_MAX_BUDGET_USD:
            raise runner.RunnerRefused(
                f"a live pilot needs {PILOT_MIN}<=N<={PILOT_MAX} and budget<={PILOT_MAX_BUDGET_USD}"
            )
    if args.prior_ledger is not None:
        check_prior_ledger(args.spend_ledger, args.prior_ledger)
    elif not args.no_prior_paid_run and not args.dry_run:
        raise runner.RunnerRefused("live runs need --prior-ledger or --no-prior-paid-run")
    resume: Path | None = args.resume_from
    if resume is not None:
        if args.out is not None and args.out != resume:
            raise runner.RunnerRefused("--out must be omitted or equal --resume-from")
        if pilot is not None or args.max_jobs is not None:
            raise runner.RunnerRefused("--resume-from excludes --pilot-jobs and --max-jobs")
        if not (resume / "meta.json").is_file() or not (resume / "receipt.jsonl").is_file():
            raise runner.RunnerRefused(f"{resume} has no meta.json and receipt.jsonl to resume")
    elif args.out is None:
        raise runner.RunnerRefused("--out is required unless --resume-from is given")
    check_budget(args.budget_usd, args.spend_ledger, args.ledger_rows)
    out: Path = resume if resume is not None else args.out
    if (out / "meta.json").exists():  # never mix pilot and scored records in one directory
        was_pilot = json.loads((out / "meta.json").read_text()).get("pilot") is True
        if was_pilot != (pilot is not None):
            raise runner.RunnerRefused(f"{out} holds a different kind of run (pilot vs scored)")
    ledger = args.spend_ledger
    if args.dry_run:
        out.mkdir(parents=True, exist_ok=True)
        ledger = out / "dry_run_spend_ledger.jsonl"  # the real ledger is never touched
        shutil.copyfile(args.spend_ledger, ledger)
        client: xai.XaiClient = DryRunClient()
    else:
        key = env.get(KEY_ENV, "")
        if not key:
            raise runner.RunnerRefused(f"{KEY_ENV} is not set")
        if not xai.WIRE_CONFIRMED:  # else every run would be recorded as a RUN_ERROR
            raise runner.RunnerRefused("xai.WIRE_CONFIRMED is False: wire format unconfirmed")
        client = xai.LiveClient(key)
    all_tasks = tasks.default_tasks()
    jobs = runner.plan_matrix(all_tasks, xai.MODEL_CONFIGS, CONDITION_SETS[args.condition_set])
    if resume is not None:
        failed = runner.failed_run_ids(resume / "receipt.jsonl")
        jobs = tuple(j for j in jobs if j.job_id in failed)
        if not jobs:
            raise runner.RunnerRefused(f"{resume} has no RUN_ERROR job in this condition set")
    if args.max_jobs is not None:
        jobs = jobs[: args.max_jobs]
    if pilot is not None:
        jobs = runner.pilot_subset(jobs, pilot)
    code = runner.run_matrix(
        jobs,
        {t.task_id: t for t in all_tasks},
        client,
        out_dir=out,
        protocol=args.repo / "docs" / "PREREGISTRATION.md",
        repo=args.repo,
        cap_usd=args.budget_usd,
        pricing=xai.PRICING,
        clock=lambda: datetime.now(UTC),
        spend_ledger=ledger,
        extra_meta={"pilot": True, "pilot_jobs": pilot} if pilot is not None else None,
    )
    print(f"{code.name}: see {out / 'result.json'}")
    return int(code)


if __name__ == "__main__":
    sys.exit(main())
