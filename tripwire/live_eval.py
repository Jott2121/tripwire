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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tripwire.live_eval")
    p.add_argument("--condition-set", choices=sorted(CONDITION_SETS), required=True)
    p.add_argument("--budget-usd", type=float, required=True)
    p.add_argument("--confirm", required=True, help=f"must equal {CONFIRM_PHRASE}")
    p.add_argument("--ledger-rows", type=int, required=True, help="spend rows you expect")
    p.add_argument("--spend-ledger", type=Path, default=Path("runs/spend_ledger.jsonl"))
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--repo", type=Path, default=Path("."))
    p.add_argument("--dry-run", action="store_true", help="scripted client, throwaway ledger")
    p.add_argument("--max-jobs", type=int, help="dry-run only: run the first N jobs")
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
    check_budget(args.budget_usd, args.spend_ledger, args.ledger_rows)
    out: Path = args.out
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
    if args.max_jobs is not None:
        jobs = jobs[: args.max_jobs]
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
    )
    print(f"{code.name}: see {out / 'result.json'}")
    return int(code)


if __name__ == "__main__":
    sys.exit(main())
