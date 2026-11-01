"""v2 live driver: the one entry point the manual v2 workflow calls (preregistration v2, change 8).

``python -m tripwire.live_v2 run --model grok-4.7 --stage 1 --arm scored --budget-usd 14 ...``
``python -m tripwire.live_v2 merge --base runs/spend_ledger_v2.jsonl --out merged.jsonl S1 S2``

It reuses v1's runner (receipts, resumable halts, the shared spend ledger, per-run cost) with the
v2 task set, both shadow gates, a v2 ledger and the v2 caps. Fail closed, in order:

1. The confirm phrase. A known model row, its stage, and an arm it may run (pilots are for the new
   models M2-M4, the bridge is M1 only).
2. The protocol: ``docs/PREREGISTRATION-v2-multimodel.md``, committed and unmodified (checked by
   the runner), and for a live run its status line must read ``**Status: FROZEN <date>**``. A dry
   run may use the DRAFT.
3. Prices in force today (Gemini's 2026 rate ends 2026-12-31).
4. The v2 ledger: readable, the row count the operator expects, every spend row tagged with its
   bucket, and (live) the last paid run's rows already committed.
5. The budget: at most this shard's equal share of the room left in BOTH the $260 ceiling and the
   row's own cap (``BUCKET_CAPS``), so shards run at the same time can never pass either.
6. For a scored run of M2-M4: a pilot of that row under this same protocol, with at least
   ``PILOT_JOBS`` records, no RUN_ERROR, and actual cost at most ``PILOT_COST_RATIO`` times the
   plain planning estimate (no output margin). Otherwise the study stops for an owner decision.
7. Live only: the row's own key, from the environment, read here and nowhere else.

``--dry-run`` swaps in a scripted client and a throwaway copy of the ledger, so the path runs with
no secret and no network. Shards are a stable split of the job list (see :func:`shard_jobs`); each
shard writes its own ledger copy, and :func:`merge_ledgers` joins them after the run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from tripwire import clients, runner, tasks_v2
from tripwire.harness import SHADOW_VARIANTS, Exclusion
from tripwire.live_eval import CONFIRM_PHRASE, DryRunClient, check_prior_ledger
from tripwire.prompts import Condition
from tripwire.xai import WIRE_CONFIRMED, ExecPath, Pricing, XaiError

PROTOCOL = Path("docs/PREREGISTRATION-v2-multimodel.md")
LEDGER = Path("runs/spend_ledger_v2.jsonl")
HARD_CEILING_USD = 260.0  # OWNER-DECIDED 2026-11-01: cumulative across all v2 sessions
PILOTS = "pilots+bridge"
BUCKET_CAPS: Mapping[str, float] = {  # the preregistered cost table's caps
    "M1": 87.0,
    "M2": 110.0,
    "M3": 42.0,
    "M4": 8.0,
    PILOTS: 8.0,
}
ARMS = ("scored", "pilot", "bridge")
CONDITIONS = (Condition.BASELINE, Condition.PROMPT_FIX)  # the two live arms
PILOT_JOBS = 20
BRIDGE_JOBS = 24
PILOT_COST_RATIO = 1.5
OUTPUT_MARGIN = 1.5  # the caps assume 1.5x the planning output tokens
PROFILE: Mapping[Condition, tuple[int, int]] = {  # planning tokens per run: (input, output)
    Condition.BASELINE: (9_400, 1_600),
    Condition.PROMPT_FIX: (45_000, 6_400),
    Condition.GATE: (45_000, 6_400),  # the bridge: prompt_fix plus a gate the model never sees
}
_STATUS = re.compile(r"^\*\*Status: (FROZEN (\d{4}-\d{2}-\d{2})|DRAFT)\b")

assert sum(BUCKET_CAPS.values()) <= HARD_CEILING_USD


def condition_estimate(
    condition: Condition, price: Pricing, margin: float = OUTPUT_MARGIN
) -> float:
    """Planning cost of one run at list price, no cache discount. The budget checks use the caps'
    output margin; the pilot check compares against the plain profile (``margin=1``)."""
    n_in, n_out = PROFILE[condition]
    return price.cost(n_in, round(n_out * margin))


def estimate(job: runner.Job, price: Pricing) -> float:
    return condition_estimate(job.condition, price)


def protocol_status(text: str) -> tuple[str, date | None]:
    """``("FROZEN", date)`` or ``("DRAFT", None)``; anything else (none, or two) raises."""
    found = [m for m in map(_STATUS.match, text.splitlines()) if m]
    if len(found) != 1:
        raise runner.RunnerRefused(f"protocol needs exactly one status line, found {len(found)}")
    m = found[0]
    if m.group(2) is None:
        return "DRAFT", None
    return "FROZEN", date.fromisoformat(m.group(2))


# --- jobs -----------------------------------------------------------------------------------


def scored_jobs(row: clients.ModelRow) -> tuple[runner.Job, ...]:
    """The 480 frozen slots under both live arms: 960 client-side jobs, one repeat."""
    return tuple(
        runner.Job(slot, row.config, ExecPath.CLIENT_SIDE, cond, 1)
        for slot in tasks_v2.default_plan()
        for cond in CONDITIONS
    )


def arm_jobs(row: clients.ModelRow, arm: str) -> tuple[runner.Job, ...]:
    if arm == "scored":
        return scored_jobs(row)
    if arm == "pilot":
        return runner.pilot_subset(scored_jobs(row), PILOT_JOBS)
    gate = tuple(
        runner.Job(slot, row.config, ExecPath.CLIENT_SIDE, Condition.GATE, 1)
        for slot in tasks_v2.default_plan()
    )
    return runner.pilot_subset(gate, BRIDGE_JOBS)


def parse_shard(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d+)/(\d+)", text)
    if not m or not 1 <= int(m.group(1)) <= int(m.group(2)):
        raise runner.RunnerRefused(f"--shard must be i/n with 1 <= i <= n, got {text!r}")
    return int(m.group(1)), int(m.group(2))


def shard_jobs(jobs: Sequence[runner.Job], i: int, n: int) -> tuple[runner.Job, ...]:
    """Shard ``i`` of ``n``: per condition, jobs in job-id hash order dealt round-robin.

    Stable (depends only on the job ids), disjoint, covering, and balanced per condition, so each
    shard's planning estimate is within one job of an equal share."""
    chosen: set[str] = set()
    for cond in dict.fromkeys(j.condition for j in jobs):
        ids = sorted(
            (j.job_id for j in jobs if j.condition is cond),
            key=lambda x: hashlib.sha256(f"shard:{x}".encode()).hexdigest(),
        )
        chosen.update(ids[i - 1 :: n])
    return tuple(j for j in jobs if j.job_id in chosen)


def bucket_of(row: clients.ModelRow, arm: str) -> str:
    return row.slot if arm == "scored" else PILOTS


# --- ledger -----------------------------------------------------------------------------------


def _ledger_lines(path: Path) -> list[str]:
    runner.read_spend(path)  # fails closed on a missing or corrupt ledger
    return path.read_text().splitlines()


def _bucket_totals(lines: Sequence[str], where: object) -> dict[str, float]:
    totals = dict.fromkeys(BUCKET_CAPS, 0.0)
    for line in lines[1:]:
        row = json.loads(line)
        if row.get("study") != "v2" or row.get("bucket") not in totals:
            raise runner.SpendLedgerError(f"v2 spend row without a known bucket: {where}")
        totals[row["bucket"]] += float(row["cost_usd"])
    return totals


def spend_by_bucket(path: Path) -> dict[str, float]:
    """USD per bucket. A spend row without a known bucket tag fails closed."""
    return _bucket_totals(_ledger_lines(path), path)


def room_usd(path: Path, bucket: str, shards: int) -> float:
    """This shard's equal share of the room left in both the ceiling and the bucket's cap."""
    by_bucket = spend_by_bucket(path)
    room = min(HARD_CEILING_USD - sum(by_bucket.values()), BUCKET_CAPS[bucket] - by_bucket[bucket])
    return max(0.0, room) / shards


def merge_ledgers(base: Path, shard_ledgers: Sequence[Path]) -> tuple[str, dict[str, float]]:
    """The base ledger plus every shard's new rows, in the order given; and the bucket totals.

    Each shard ledger must start with the base ledger exactly (it ran from that copy) and add only
    v2 spend rows; a row that appears in two shards fails closed."""
    head = _ledger_lines(base)
    merged = list(head)
    seen: set[str] = set()
    for path in shard_ledgers:
        lines = _ledger_lines(path)
        if lines[: len(head)] != head:
            raise runner.RunnerRefused(f"{path} does not extend the base ledger exactly")
        for line in lines[len(head) :]:
            if line in seen:
                raise runner.RunnerRefused(f"{path} repeats a spend row from another shard")
            seen.add(line)
            merged.append(line)
    return "\n".join(merged) + "\n", _bucket_totals(merged, "merged ledger")


# --- pilot gate ------------------------------------------------------------------------------


def check_pilot(pilot_dir: Path, row: clients.ModelRow, protocol_sha256: str) -> float:
    """Return the pilot's actual/estimate cost ratio, or raise. Live and dry alike."""
    meta_path, receipt = pilot_dir / "meta.json", pilot_dir / "receipt.jsonl"
    if not meta_path.is_file() or not receipt.is_file():
        raise runner.RunnerRefused(f"{pilot_dir} has no pilot meta.json and receipt.jsonl")
    meta = json.loads(meta_path.read_text())
    if meta.get("arm") != "pilot" or meta.get("slot") != row.slot:
        raise runner.RunnerRefused(f"{pilot_dir} is not a {row.slot} pilot")
    if meta.get("protocol_sha256") != protocol_sha256:
        raise runner.RunnerRefused("the pilot ran under a different protocol version")
    latest: dict[str, dict[str, object]] = {}
    for line in receipt.read_text().splitlines():
        if line:
            rec = json.loads(line)
            latest[rec["run_id"]] = rec
    recs = list(latest.values())
    if len(recs) < PILOT_JOBS:
        raise runner.RunnerRefused(f"pilot has {len(recs)} records, needs {PILOT_JOBS}")
    if any(r["exclusion"] == Exclusion.RUN_ERROR.value for r in recs):
        raise runner.RunnerRefused("pilot has RUN_ERROR records: the client is not confirmed")
    if any(r["model_label"] != row.config.label or r["cost_usd"] is None for r in recs):
        raise runner.RunnerRefused("pilot records are for another model or have no cost")
    # fsum: the same result on every Python (3.12 made sum() of floats compensated, 3.11 is not)
    est = math.fsum(condition_estimate(Condition(str(r["condition"])), row.pricing, 1.0)
                    for r in recs)  # fmt: skip
    ratio = math.fsum(float(str(r["cost_usd"])) for r in recs) / est
    if ratio > PILOT_COST_RATIO + 1e-9:  # inclusive: absorb float rounding only
        raise runner.RunnerRefused(
            f"pilot cost is {ratio:.2f}x the estimate (> {PILOT_COST_RATIO}): owner decision"
        )
    return ratio


# --- CLI -------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tripwire.live_v2")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one model row's arm (or one shard of it)")
    r.add_argument("--model", required=True, help="a v2 row label, e.g. grok-4.7")
    r.add_argument("--stage", type=int, required=True, choices=(1, 2))
    r.add_argument("--arm", choices=ARMS, default="scored")
    r.add_argument("--budget-usd", type=float, required=True)
    r.add_argument("--confirm", required=True, help=f"must equal {CONFIRM_PHRASE}")
    r.add_argument("--ledger-rows", type=int, required=True, help="spend rows you expect")
    r.add_argument("--spend-ledger", type=Path, default=LEDGER)
    r.add_argument("--out", type=Path, required=True, help="this run's (or shard's) directory")
    r.add_argument("--shard", default="1/1", help="i/n: run shard i of n (scored arm only)")
    r.add_argument("--pilot-from", type=Path, help="this row's pilot directory (M2-M4 scored)")
    r.add_argument("--repo", type=Path, default=Path("."))
    r.add_argument("--dry-run", action="store_true", help="scripted client, throwaway ledger")
    r.add_argument("--max-jobs", type=int, help="dry-run only: run the first N jobs")
    prior = r.add_mutually_exclusive_group()
    prior.add_argument("--prior-ledger", type=Path, help="the last paid v2 run's spend ledger")
    prior.add_argument("--no-prior-paid-run", action="store_true")
    m = sub.add_parser("merge", help="join shard ledgers onto the committed base ledger")
    m.add_argument("--base", type=Path, default=LEDGER)
    m.add_argument("--out", type=Path, required=True)
    m.add_argument("shard_ledgers", type=Path, nargs="+")
    return p


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    today: date | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    env = os.environ if env is None else env
    try:
        if args.cmd == "merge":
            return _merge(args)
        return _run(args, env, today or datetime.now(UTC).date())
    except (runner.RunnerRefused, XaiError, OSError, KeyError) as exc:
        print(f"REFUSED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return int(runner.ExitCode.REFUSED)


def _merge(args: argparse.Namespace) -> int:
    text, totals = merge_ledgers(args.base, args.shard_ledgers)
    args.out.write_text(text)
    over = {b: t for b, t in totals.items() if t > BUCKET_CAPS[b] + 1e-9}
    for bucket, total in totals.items():
        print(f"{bucket}: {total:.4f} of {BUCKET_CAPS[bucket]:.2f} USD")
    if over or sum(totals.values()) > HARD_CEILING_USD + 1e-9:
        print(
            f"OVER CAP (merged anyway, the ledger records what was paid): {over}", file=sys.stderr
        )
        return 1
    return 0


def _run(args: argparse.Namespace, env: Mapping[str, str], today: date) -> int:
    if args.confirm != CONFIRM_PHRASE:
        raise runner.RunnerRefused("confirm string does not match")
    row = clients.v2_row(args.model)
    if args.stage != row.stage:
        raise runner.RunnerRefused(f"{row.slot} runs in stage {row.stage}, not {args.stage}")
    if args.arm == "pilot" and row.slot == "M1":
        raise runner.RunnerRefused("M1 has no pilot (v1 measured it); run its scored arm")
    if args.arm == "bridge" and row.slot != "M1":
        raise runner.RunnerRefused("the bridge arm is M1 only")
    i, n = parse_shard(args.shard)
    if n > 1 and args.arm != "scored":
        raise runner.RunnerRefused("only the scored arm is sharded")
    if args.max_jobs is not None and not args.dry_run:
        raise runner.RunnerRefused("--max-jobs is dry-run only")
    protocol = args.repo / PROTOCOL
    status, frozen = protocol_status(protocol.read_text())
    if not args.dry_run and (status != "FROZEN" or frozen is None or frozen > today):
        raise runner.RunnerRefused("live v2 runs need the protocol status FROZEN <date>")
    if not row.pricing.in_force(today):
        raise runner.RunnerRefused(f"{row.config.label} prices expired {row.pricing.valid_until}")
    if args.prior_ledger is not None:
        check_prior_ledger(args.spend_ledger, args.prior_ledger)
    elif not args.no_prior_paid_run and not args.dry_run:
        raise runner.RunnerRefused("live runs need --prior-ledger or --no-prior-paid-run")
    rows = len(_ledger_lines(args.spend_ledger)) - 1
    if rows != args.ledger_rows:
        raise runner.RunnerRefused(
            f"ledger has {rows} spend rows, operator expected {args.ledger_rows}"
        )
    bucket = bucket_of(row, args.arm)
    share = room_usd(args.spend_ledger, bucket, n)
    if not 0 < args.budget_usd <= share:
        raise runner.RunnerRefused(
            f"budget {args.budget_usd} USD must be in (0, {share:.2f}]: shard {i}/{n}'s share of "
            f"the room left in {bucket}'s cap and the {HARD_CEILING_USD:.0f} USD ceiling"
        )
    if args.arm == "scored" and row.slot != "M1":
        if args.pilot_from is None:
            raise runner.RunnerRefused(f"{row.slot}'s scored run needs --pilot-from its pilot")
        digest = hashlib.sha256(protocol.read_bytes()).hexdigest()
        check_pilot(args.pilot_from, row, digest)
    meta = {"study": "v2", "arm": args.arm, "slot": row.slot, "shard": f"{i}/{n}",
            "pilot": args.arm == "pilot"}  # fmt: skip
    if (args.out / "meta.json").exists():  # resuming: never mix arms, rows or shards
        old = json.loads((args.out / "meta.json").read_text())
        if any(old.get(k) != v for k, v in meta.items()):
            raise runner.RunnerRefused(f"{args.out} holds a different v2 arm, row or shard")
    ledger = args.spend_ledger
    if args.dry_run:
        args.out.mkdir(parents=True, exist_ok=True)
        ledger = args.out / "dry_run_spend_ledger_v2.jsonl"  # the real ledger is never touched
        shutil.copyfile(args.spend_ledger, ledger)
        client: clients.ModelClient = DryRunClient()
    else:
        key = env.get(row.key_env, "")
        if not key:
            raise runner.RunnerRefused(f"{row.key_env} is not set")
        if row.provider == "xai" and not WIRE_CONFIRMED:
            raise runner.RunnerRefused("xai.WIRE_CONFIRMED is False: wire format unconfirmed")
        client = clients.make_client(row, key)
    jobs = shard_jobs(arm_jobs(row, args.arm), i, n)
    if args.max_jobs is not None:
        jobs = jobs[: args.max_jobs]
    shadow = () if args.arm == "bridge" else SHADOW_VARIANTS
    code = runner.run_matrix(
        jobs,
        {t.task_id: t for t in tasks_v2.default_tasks()},
        client,
        out_dir=args.out,
        protocol=protocol,
        repo=args.repo,
        cap_usd=args.budget_usd,
        pricing={row.config.label: row.pricing},
        clock=lambda: datetime.now(UTC),
        spend_ledger=ledger,
        ceiling_usd=runner.read_spend(ledger) + args.budget_usd,
        hard_ceiling_usd=HARD_CEILING_USD,
        extra_meta=meta,
        estimate=estimate,
        run_kwargs={"task_set": "v2", "shadow_gates": shadow},
        spend_tags={
            "study": "v2",
            "bucket": bucket,
            "slot": row.slot,
            "arm": args.arm,
            "shard": f"{i}/{n}",
        },  # fmt: skip
    )
    print(f"{code.name}: see {args.out / 'result.json'}")
    return int(code)


if __name__ == "__main__":
    sys.exit(main())
