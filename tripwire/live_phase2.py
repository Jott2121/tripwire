"""Phase two live runner (``docs/PREREGISTRATION-phase2-real-data.md``): the one entry point that
spends money on phase two. Run on the owner's machine, against the files the freeze used.

    PYTHONPATH=. python3 -m tripwire.live_phase2 run --model muse-spark-open --arm pilot \\
        --budget-usd 2 --confirm I-APPROVE-LIVE-SPEND [--dry-run]
    PYTHONPATH=. python3 -m tripwire.live_phase2 report --out reports/phase2_results.json

``--master``, ``--acftref``, ``--sdr`` and ``--items`` default to the paths the freeze used.

Fail closed, in this order, before any call:

1. A known model row (``clients.PHASE2_MODELS``) and arm (``pilot`` or ``scored``); live runs
   need the confirm phrase.
2. The protocol's status line reads ``**Status: FROZEN <date>**`` (a dry run may use a DRAFT), and
   the protocol text carries the freeze record's rules fingerprint and item-list sha256.
3. The freeze record (``reports/phase2_freeze.json``): today's ``mining.fingerprint()``, the sha256
   of every input file, and the item list rebuilt from ``--items`` all match it.
4. The model's price is in force today.
5. The budget: this run's ``--budget-usd`` plus what the spend file already holds stays within
   the model's cap (``MODEL_CAPS``) and the study's ``TOTAL_CAP_USD`` ($65, owner-approved
   2026-11-01). During the run, a job starts only if its planning estimate (list price, no cache,
   1.5x output) still fits the budget; otherwise the run halts, resumable.
6. A scored run needs this model's pilot: ``PILOT_JOBS`` records, no RUN_ERROR, every cost known.
7. Live only: the row's API key from the environment (``XAI_API_KEY`` or ``OPENROUTER_API_KEY``),
   read here and nowhere else, never printed or written.

Records (``RunRecord`` JSONL; answers can hold real serials) and the spend file stay under
``data/phase2/runs/`` (gitignored). A run with an unknown cost halts the run; a quota or rate
refusal halts it, booking what was paid. Re-running the same command resumes: jobs already
recorded are skipped. ``report`` writes rates and counts only, safe to commit.

``--dry-run`` swaps in a scripted agent (the rules-as-reference policy) and a throwaway output
directory: no key, no network, no spend, but every file check above runs on the real files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from tripwire import clients, mining, phase2, registry
from tripwire.sdr import SDRFormatError, load_sdr_csv
from tripwire.xai import LiveCallsDisabled, QuotaExhausted, XaiClient

CONFIRM_PHRASE = "I-APPROVE-LIVE-SPEND"
PROTOCOL = Path("docs/PREREGISTRATION-phase2-real-data.md")
FREEZE = Path("reports/phase2_freeze.json")
ITEMS = Path("data/phase2/items.json")
RUNS = Path("data/phase2/runs")
DEFAULT_INPUTS = {
    "master": Path("data/raw/registry/MASTER.txt"),
    "acftref": Path("data/raw/registry/ACFTREF.txt"),
    "sdr": Path("data/raw/SDR-2026.csv"),
}
TOTAL_CAP_USD = 65.0  # OWNER-APPROVED 2026-11-01, the whole study, pilots included
MODEL_CAPS: Mapping[str, float] = {"grok-4.7": 38.0, "muse-spark-open": 27.0}
ARMS = ("pilot", "scored")
CONDITIONS = (phase2.Condition.BASELINE, phase2.Condition.PROMPT_FIX)
PILOT_JOBS = 20  # 10 per condition, to confirm the wire before any scored run
OUTPUT_MARGIN = 1.5
PROFILE: Mapping[phase2.Condition, tuple[int, int]] = {  # planning tokens per run (in, out)
    phase2.Condition.BASELINE: (9_400, 1_600),
    phase2.Condition.PROMPT_FIX: (45_000, 6_400),
}
_STATUS = re.compile(r"^\*\*Status: (FROZEN (\d{4}-\d{2}-\d{2})|DRAFT)\b", re.M)

assert sum(MODEL_CAPS.values()) <= TOTAL_CAP_USD


class Refused(RuntimeError):
    """A check failed before any call: nothing ran and nothing was spent."""


Job = tuple[phase2.Task, phase2.Condition]


def estimate(condition: phase2.Condition, row: clients.ModelRow) -> float:
    i, o = PROFILE[condition]
    return row.pricing.cost(i, round(o * OUTPUT_MARGIN))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_protocol(text: str, record: Mapping[str, Any], *, live: bool) -> None:
    m = _STATUS.search(text)
    if m is None:
        raise Refused(f"{PROTOCOL} has no status line")
    if live and not m.group(1).startswith("FROZEN"):
        raise Refused(f"{PROTOCOL} is not FROZEN; a live run needs the frozen protocol")
    for key in (record["rules"]["fingerprint"], record["item_list_sha256"]):
        if key not in text:
            raise Refused(f"{PROTOCOL} does not carry the freeze record's {key[:12]}...")


def load_tasks(items: Path, record: Mapping[str, Any]) -> list[phase2.Task]:
    """The frozen tasks, rebuilt from the local items file and checked against the record."""
    raw = json.loads(items.read_text())
    tasks = phase2.build_tasks(mining.Item(**t["item"]) for t in raw["tasks"])
    sha = phase2.item_list_sha256(tasks)
    if len(tasks) != record["tasks"] or sha != record["item_list_sha256"]:
        raise Refused(f"{items} does not rebuild to the frozen item list")
    return tasks


def check_inputs(paths: Mapping[str, Any], record: Mapping[str, Any]) -> None:
    if mining.fingerprint() != record["rules"]["fingerprint"]:
        raise Refused("tripwire/mining.py changed since the freeze (fingerprint differs)")
    want = {"master": [record["inputs"]["master"]], "acftref": [record["inputs"]["acftref"]],
            "sdr": record["inputs"]["sdr"]}  # fmt: skip
    for role, entries in want.items():
        got = paths[role] if isinstance(paths[role], list) else [paths[role]]
        if [_sha256(p) for p in got] != [e["sha256"] for e in entries]:
            raise Refused(f"the {role} file is not the one the freeze used (sha256 differs)")


def build_sources(paths: Mapping[str, Any]) -> phase2.Sources:
    reg = registry.Registry.build(
        registry.load_master(paths["master"]).records,
        registry.load_reference(paths["acftref"]).records,
    )
    rows = [r for p in paths["sdr"] for r in load_sdr_csv(p).records]
    return phase2.Sources.build(rows, reg)


def jobs_for(tasks: Sequence[phase2.Task], arm: str) -> list[Job]:
    """Pilot: ``PILOT_JOBS`` tasks, half per condition. Scored: every task in both conditions,
    interleaved in a fixed hashed order so a halt part-way does not tilt one condition."""

    def key(salt: str, t: phase2.Task, c: str = "") -> str:
        return hashlib.sha256(f"{salt}:{t.task_id}:{c}".encode()).hexdigest()

    if arm == "pilot":
        picked = sorted(tasks, key=lambda t: key("pilot", t))[:PILOT_JOBS]
        half = len(picked) // 2
        return [(t, CONDITIONS[0]) for t in picked[:half]] + [
            (t, CONDITIONS[1]) for t in picked[half:]
        ]
    jobs = [(t, c) for t in tasks for c in CONDITIONS]
    return sorted(jobs, key=lambda j: key("order", j[0], j[1].value))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def spent(spend_file: Path, label: str | None = None) -> float:
    return sum(r["cost_usd"] for r in _read_jsonl(spend_file) if label in (None, r["model"]))


def check_budget(spend_file: Path, label: str, budget: float) -> None:
    if budget <= 0:
        raise Refused("--budget-usd must be positive")
    have, total = spent(spend_file, label), spent(spend_file)
    if have + budget > MODEL_CAPS[label] + 1e-9:
        raise Refused(f"{label}: ${have:.2f} spent + ${budget:.2f} asked exceeds its "
                      f"${MODEL_CAPS[label]:.2f} cap")  # fmt: skip
    if total + budget > TOTAL_CAP_USD + 1e-9:
        raise Refused(f"${total:.2f} spent + ${budget:.2f} asked exceeds the study's "
                      f"${TOTAL_CAP_USD:.2f} cap")  # fmt: skip


def check_pilot(runs_dir: Path, label: str) -> None:
    recs = _read_jsonl(runs_dir / label / "pilot.jsonl")
    if len(recs) < PILOT_JOBS:
        raise Refused(f"{label}: the pilot has {len(recs)} of {PILOT_JOBS} runs; run it first")
    if any(r["exclusion"] for r in recs) or any(r["cost_usd"] is None for r in recs):
        raise Refused(f"{label}: the pilot has a RUN_ERROR or an unknown cost; owner decision")


def _record_from(d: Mapping[str, Any]) -> phase2.RunRecord:
    fixed = {k: tuple(v) for k, v in d.items() if k in ("served_models", "tool_calls")}
    return phase2.RunRecord(**{**d, **fixed})


def execute(
    jobs: Sequence[Job],
    row: clients.ModelRow,
    arm: str,
    sources: phase2.Sources,
    client_for: Callable[[phase2.Task], XaiClient],
    out: Path,
    spend_file: Path,
    budget: float,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Run the jobs not yet recorded, within ``budget``. Returns a summary; ``halted`` names why
    it stopped early (budget, unknown cost, quota), or is None when every job ran."""
    out.parent.mkdir(parents=True, exist_ok=True)
    done = {(r["task_id"], r["condition"]) for r in _read_jsonl(out)}
    session, ran, halted = 0.0, 0, None
    label = row.config.label

    def book(run_id: str, cost: float) -> None:
        with spend_file.open("a") as f:
            f.write(json.dumps({"run_id": run_id, "model": label, "arm": arm,
                                "cost_usd": cost, "at": now().isoformat()}) + "\n")  # fmt: skip

    for task, cond in jobs:
        if (task.task_id, cond.value) in done:
            continue
        if session + estimate(cond, row) > budget + 1e-9:
            halted = f"budget: ${session:.2f} spent of ${budget:.2f}; the next run may not fit"
            break
        run_id = f"{label}:{arm}:{task.task_id}:{cond.value}"
        try:
            rec = phase2.run_item(task, sources, client_for(task), row.config, cond,
                                  run_id=run_id, pricing=row.pricing)  # fmt: skip
        except QuotaExhausted as exc:
            paid = getattr(exc, "paid_usd", 0.0) or 0.0
            if paid:
                book(run_id + ":halted", paid)
                session += paid
            halted = f"quota or rate limit: {exc}"
            break
        with out.open("a") as f:
            f.write(json.dumps(asdict(rec), sort_keys=True) + "\n")
        ran += 1
        cost = rec.cost_usd if rec.cost_usd is not None else estimate(cond, row)
        book(run_id, cost)  # an unknown cost is booked at the planning estimate, then halts
        session += cost
        if rec.cost_usd is None:
            halted = "a run's cost is unknown; stopped so the cap cannot be passed unseen"
            break
    left = sum(1 for t, c in jobs if (t.task_id, c.value) not in done) - ran
    return {"model": label, "arm": arm, "ran": ran, "left": left,
            "session_usd": round(session, 4), "halted": halted}  # fmt: skip


def report(runs_dir: Path) -> dict[str, Any]:
    """Rates and counts per model and condition (no item values), plus the rules-as-gate (Q4)."""
    out: dict[str, Any] = {"spend_usd": round(spent(runs_dir / "spend.jsonl"), 4), "models": {}}
    for label in MODEL_CAPS:
        recs = [_record_from(d) for d in _read_jsonl(runs_dir / label / "scored.jsonl")]
        if not recs:
            continue
        per: dict[str, Any] = {}
        for cond in CONDITIONS:
            rs = [r for r in recs if r.condition == cond.value]
            per[cond.value] = {
                "runs": len(rs),
                "run_errors": sum(1 for r in rs if r.exclusion),
                "summary": phase2.summarize(phase2.scores_of(rs)),
                "tokens": {"input": sum(r.input_tokens for r in rs),
                           "output": sum(r.output_tokens for r in rs)},
            }  # fmt: skip
        ref = [phase2.Score(**r.reference) for r in recs if r.reference is not None]
        out["models"][label] = {
            "conditions": per,
            "rules_as_gate": phase2.summarize(ref),
            "spend_usd": round(spent(runs_dir / "spend.jsonl", label), 4),
        }
    return out


def _paths(args: argparse.Namespace) -> dict[str, Any]:
    return {"master": args.master, "acftref": args.acftref, "sdr": args.sdr}


def run(args: argparse.Namespace, environ: Mapping[str, str] = os.environ,
        today: date | None = None) -> int:  # fmt: skip
    try:
        row = clients.phase2_row(args.model)
    except KeyError:
        print(f"REFUSED: unknown model {args.model!r}", file=sys.stderr)
        return 2
    live = not args.dry_run
    try:
        if live and args.confirm != CONFIRM_PHRASE:
            raise Refused(f"a live run needs --confirm {CONFIRM_PHRASE}")
        record = json.loads(args.freeze.read_text())
        check_protocol(args.protocol.read_text(), record, live=live)
        check_inputs(_paths(args), record)
        tasks = load_tasks(args.items, record)
        if not row.pricing.in_force(today or date.today()):
            raise Refused(f"{row.config.label}'s price is no longer in force; re-check it")
        runs_dir = args.runs_dir if live else Path(tempfile.mkdtemp(prefix="phase2-dry-"))
        runs_dir.mkdir(parents=True, exist_ok=True)
        spend_file = runs_dir / "spend.jsonl"
        check_budget(spend_file, row.config.label, args.budget_usd)
        if args.arm == "scored" and live:
            check_pilot(runs_dir, row.config.label)
        sources = build_sources(_paths(args))
        if live:
            key = environ.get(row.key_env, "")
            if not key:
                raise Refused(f"{row.key_env} is not set in this shell")
            client = clients.make_client(row, key)

            def client_for(_task: phase2.Task) -> XaiClient:
                return client
        else:

            def client_for(task: phase2.Task) -> XaiClient:
                return phase2.scripted_client("oracle", task, sources)

    except (Refused, LiveCallsDisabled, phase2.Phase2Error, registry.RegistryFormatError,
            SDRFormatError, OSError, KeyError, ValueError) as exc:  # fmt: skip
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    jobs = jobs_for(tasks, args.arm)
    out = runs_dir / row.config.label / f"{args.arm}.jsonl"
    summary = execute(jobs, row, args.arm, sources, client_for, out, spend_file, args.budget_usd)
    summary |= {"dry_run": not live, "records": str(out)}
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 3 if summary["halted"] else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one model's pilot or scored arm")
    r.add_argument("--model", required=True, choices=sorted(MODEL_CAPS))
    r.add_argument("--arm", required=True, choices=ARMS)
    r.add_argument("--budget-usd", type=float, required=True)
    r.add_argument("--confirm", default="")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--master", type=Path, default=DEFAULT_INPUTS["master"])
    r.add_argument("--acftref", type=Path, default=DEFAULT_INPUTS["acftref"])
    r.add_argument("--sdr", type=Path, action="append", default=None)
    r.add_argument("--items", type=Path, default=ITEMS)
    r.add_argument("--freeze", type=Path, default=FREEZE)
    r.add_argument("--protocol", type=Path, default=PROTOCOL)
    r.add_argument("--runs-dir", type=Path, default=RUNS)
    rep = sub.add_parser("report", help="rates and counts from the scored records")
    rep.add_argument("--runs-dir", type=Path, default=RUNS)
    rep.add_argument("--out", type=Path, default=None)
    args = p.parse_args(argv)
    if args.cmd == "run":
        args.sdr = args.sdr or [DEFAULT_INPUTS["sdr"]]
        return run(args)
    text = json.dumps(report(args.runs_dir), indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    print(text, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
