"""Results report (M7 part 2): ``python -m tripwire.report <run_dir>... [--out reports]``.

Reads only the run directories' ``receipt.jsonl`` / ``meta.json`` / ``result.json``; every number
is computed from those records, none is typed here. Rules come from ``docs/PREREGISTRATION.md``:

- Headline: per fault and condition, correct runs / faulted runs, as a **pair**: the *strict
  floor* (no exclusions: excluded runs stay in the denominator and count as incorrect) and the
  *published number* (excluded runs dropped). Beside it the clean-run false-flag rate and the cost
  per correct outcome, each with a 95% CI.
- CIs: replicate-cluster bootstrap, resampling whole **tasks** (all of a task's runs in the cell
  move together), percentile interval, fixed seed and resample count recorded in the output.
- Exclusions: ``RUN_ERROR`` and ``INJECTION_UNVERIFIED`` come from the receipt; ``BASELINE_FAIL``
  is applied here (a faulted run whose task has clean runs in the same model, path and condition,
  none of them correct). Priority ``RUN_ERROR > BASELINE_FAIL > INJECTION_UNVERIFIED``.
- Cost is ``None`` (never estimated) in any cell where one run has an unknown cost.
"""

from __future__ import annotations

import argparse
import json
import random
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

BOOTSTRAP_RESAMPLES = 10_000  # chosen in M7, recorded in the deviations log before any live run
BOOTSTRAP_SEED = 1
ALPHA = 0.05
FAULTS = ("F1", "F2", "F3", "F4", "F5", "F6")
ALL = "ALL"  # all faulted runs of a condition, pooled
BASELINE_FAIL = "BASELINE_FAIL"
GATE_COMPARATOR = "prompt_fix"  # the gate's prompt is prompt_fix plus the gate
_REQUIRED = (
    "run_id",
    "task_id",
    "planned_fault",
    "condition",
    "path",
    "model_label",
    "cost_usd",
    "adjudication",
    "exclusion",
)

Cluster = Mapping[str, Sequence[tuple[float, float]]]  # task_id -> per-run (numerator, denominator)


class ReportError(ValueError):
    """The run records are unusable (fail closed: no report is written)."""


def bootstrap_ci(
    clusters: Cluster,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    alpha: float = ALPHA,
) -> dict[str, Any]:
    """Percentile CI of sum(num)/sum(den), resampling whole task clusters with replacement.

    Resamples whose denominator is zero are skipped and counted out of ``valid``. Fewer than two
    clusters, or no valid resample, gives ``lo = hi = None``: a CI is never invented.
    """
    totals = [
        (sum(n for n, _ in runs), sum(d for _, d in runs)) for _, runs in sorted(clusters.items())
    ]
    k = len(totals)
    out: dict[str, Any] = {"lo": None, "hi": None, "valid": 0, "resamples": resamples}
    if k < 2:
        return out
    rng, stats = random.Random(seed), []
    for _ in range(resamples):
        picks = [totals[rng.randrange(k)] for _ in range(k)]
        den = sum(d for _, d in picks)
        if den > 0:
            stats.append(sum(n for n, _ in picks) / den)
    if not stats:
        return out
    stats.sort()
    out["lo"] = stats[int(alpha / 2 * (len(stats) - 1))]
    out["hi"] = stats[round((1 - alpha / 2) * (len(stats) - 1))]
    out["valid"] = len(stats)
    return out


def _estimate(clusters: Cluster, **kw: Any) -> dict[str, Any]:
    num = sum(n for runs in clusters.values() for n, _ in runs)
    den = sum(d for runs in clusters.values() for _, d in runs)
    return {
        "value": num / den if den else None,
        "num": num,
        "den": den,
        **bootstrap_ci(clusters, **kw),
    }


def load_records(run_dirs: Iterable[Path]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Receipt rows from every directory, plus each directory's meta/result. Fails closed."""
    rows: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in run_dirs:
        receipt = d / "receipt.jsonl"
        if not receipt.is_file():
            raise ReportError(f"{d}: no receipt.jsonl")
        try:
            these = [json.loads(x) for x in receipt.read_text().splitlines() if x.strip()]
            meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
            result = (
                json.loads((d / "result.json").read_text()) if (d / "result.json").exists() else {}
            )
        except json.JSONDecodeError as exc:
            raise ReportError(f"{d}: unreadable record: {exc}") from exc
        for r in these:
            missing = [k for k in _REQUIRED if k not in r]
            if missing:
                raise ReportError(f"{d}: record {r.get('run_id')!r} lacks {missing}")
            if r["run_id"] in seen:
                raise ReportError(f"duplicate run_id {r['run_id']!r}")
            if r["exclusion"] is None and r["adjudication"] is None:
                raise ReportError(f"{r['run_id']}: not excluded but never adjudicated")
            seen.add(r["run_id"])
        rows += these
        sources.append({"dir": d.name, "meta": meta, "result": result, "records": len(these)})
    if not rows:
        raise ReportError("no run records")
    return rows, sources


def _correct(r: Mapping[str, Any]) -> bool:
    return bool(r["adjudication"] and r["adjudication"]["correct"])


def _false_flag(r: Mapping[str, Any]) -> bool:
    return bool(r["adjudication"] and r["adjudication"]["false_flag"])


def effective_exclusions(rows: Sequence[Mapping[str, Any]]) -> dict[str, str | None]:
    """run_id -> exclusion code, adding ``BASELINE_FAIL`` (never over a receipt code)."""
    clean: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["planned_fault"] is None and r["exclusion"] is None:
            clean[(r["task_id"], r["model_label"], r["path"], r["condition"])].append(r)
    out: dict[str, str | None] = {}
    for r in rows:
        code = r["exclusion"]
        if code is None and r["planned_fault"] is not None:
            pair = clean.get((r["task_id"], r["model_label"], r["path"], r["condition"]), [])
            if pair and not any(_correct(p) for p in pair):
                code = BASELINE_FAIL
        out[r["run_id"]] = code
    return out


def _cost(rs: Sequence[Mapping[str, Any]]) -> float | None:
    costs = [r["cost_usd"] for r in rs]
    return None if any(c is None for c in costs) else float(sum(costs))


def _by_task(
    rs: Iterable[Mapping[str, Any]], f: Callable[[Mapping[str, Any]], tuple[float, float]]
) -> Cluster:
    out: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for r in rs:
        out[r["task_id"]].append(f(r))
    return out


def _faulted_cell(
    rs: Sequence[Mapping[str, Any]], excl: Mapping[str, str | None], **kw: Any
) -> dict[str, Any]:
    kept = [r for r in rs if excl[r["run_id"]] is None]
    codes: dict[str, int] = defaultdict(int)
    for r in rs:
        if excl[r["run_id"]]:
            codes[excl[r["run_id"]]] += 1
    cost_all, cost_kept = _cost(rs), _cost(kept)
    correct = sum(_correct(r) for r in kept)  # an excluded run is never counted correct
    cell: dict[str, Any] = {
        "runs": len(rs),
        "excluded": dict(sorted(codes.items())),
        "kept": len(kept),
        "correct": correct,
        # strict floor: every run in the denominator, excluded ones count as incorrect
        "strict": _estimate(
            _by_task(rs, lambda r: (float(excl[r["run_id"]] is None and _correct(r)), 1.0)), **kw
        ),
        "published": _estimate(_by_task(kept, lambda r: (float(_correct(r)), 1.0)), **kw),
        "turn_cap_hit": sum(bool(r.get("turn_cap_hit")) for r in rs),
    }
    for key, group, total in (("strict", rs, cost_all), ("published", kept, cost_kept)):
        if total is None or not correct:
            cell[key + "_cost_per_correct"] = {
                "value": None,
                "reason": "unknown cost or no correct run",
            }
        else:
            cell[key + "_cost_per_correct"] = _estimate(
                _by_task(
                    group,
                    lambda r: (
                        float(r["cost_usd"]),
                        float(excl[r["run_id"]] is None and _correct(r)),
                    ),
                ),
                **kw,
            )
    return cell


def _clean_cell(
    rs: Sequence[Mapping[str, Any]], excl: Mapping[str, str | None], **kw: Any
) -> dict[str, Any]:
    kept = [r for r in rs if excl[r["run_id"]] is None]
    codes: dict[str, int] = defaultdict(int)
    for r in rs:
        if excl[r["run_id"]]:
            codes[excl[r["run_id"]]] += 1
    return {
        "runs": len(rs),
        "excluded": dict(sorted(codes.items())),
        "kept": len(kept),
        "false_flags": sum(_false_flag(r) for r in kept),
        # strict floor for a "lower is better" rate is the worst case: excluded runs count as flags
        "strict": _estimate(
            _by_task(rs, lambda r: (float(excl[r["run_id"]] is not None or _false_flag(r)), 1.0)),
            **kw,
        ),
        "published": _estimate(_by_task(kept, lambda r: (float(_false_flag(r)), 1.0)), **kw),
    }


def _pair_key(r: Mapping[str, Any]) -> tuple[str, str, str, str]:
    m = re.fullmatch(
        rf"(.+)\.{re.escape(r['model_label'])}\.{re.escape(r['path'])}\.{re.escape(r['condition'])}\.k(\d+)",
        r["run_id"],
    )
    if not m:
        raise ReportError(f"{r['run_id']}: run id does not follow the job id layout")
    return m.group(1), r["model_label"], r["path"], m.group(2)


def gate_overhead(rows: Sequence[Mapping[str, Any]], **kw: Any) -> dict[str, Any] | None:
    """Gate cost over its planned-run twin in ``prompt_fix`` (same plan, model, path, repeat)."""
    gate = [r for r in rows if r["condition"] == "gate"]
    if not gate:
        return None
    base = {_pair_key(r): r for r in rows if r["condition"] == GATE_COMPARATOR}
    pairs = []
    for g in gate:
        b = base.get(_pair_key(g))
        if (
            b
            and "RUN_ERROR" not in (g["exclusion"], b["exclusion"])
            and None not in (g["cost_usd"], b["cost_usd"])
        ):
            pairs.append((g, b))
    info: dict[str, Any] = {
        "comparator": GATE_COMPARATOR,
        "gate_runs": len(gate),
        "pairs": len(pairs),
    }
    if not pairs:
        return {**info, "cost_overhead": None}
    clusters: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for g, b in pairs:
        clusters[g["task_id"]].append((g["cost_usd"] - b["cost_usd"], b["cost_usd"]))
    gates = [g["gate"] or {} for g, _ in pairs]
    return {
        **info,
        "cost_overhead": _estimate(clusters, **kw),
        "extra_tool_calls_per_run": sum(x.get("extra_tool_calls", 0) for x in gates) / len(gates),
        "added_latency_s_per_run": sum(x.get("latency_s", 0.0) for x in gates) / len(gates),
        "added_tokens": sum(x.get("added_tokens", 0) for x in gates),
    }


def analyse(
    rows: Sequence[Mapping[str, Any]], sources: Sequence[Mapping[str, Any]], **kw: Any
) -> dict[str, Any]:
    excl = effective_exclusions(rows)
    conds = sorted({r["condition"] for r in rows})
    cells: list[dict[str, Any]] = []
    for cond in conds:
        in_cond = [r for r in rows if r["condition"] == cond]
        for fault in (*FAULTS, ALL):
            rs = [
                r
                for r in in_cond
                if (r["planned_fault"] is not None if fault == ALL else r["planned_fault"] == fault)
            ]
            if rs:
                cells.append({"condition": cond, "fault": fault, **_faulted_cell(rs, excl, **kw)})
        clean = [r for r in in_cond if r["planned_fault"] is None]
        if clean:
            cells.append({"condition": cond, "fault": "clean", **_clean_cell(clean, excl, **kw)})
    codes: dict[str, int] = defaultdict(int)
    for c in excl.values():
        if c:
            codes[c] += 1
    return {
        "params": {
            "bootstrap": "replicate-cluster (tasks), percentile",
            "resamples": kw.get("resamples", BOOTSTRAP_RESAMPLES),
            "seed": kw.get("seed", BOOTSTRAP_SEED),
            "ci": 1 - ALPHA,
        },
        "sources": list(sources),
        "pilot": any(s["meta"].get("pilot") or s["result"].get("pilot") for s in sources),
        "incomplete": [
            s["dir"]
            for s in sources
            if s["result"].get("exit_name") not in (None, "OK", "RUN_ERRORS")
        ],
        "records": len(rows),
        "exclusions_by_code": dict(sorted(codes.items())),
        "spend_usd": _cost(list(rows)),
        "cells": cells,
        "gate_overhead": gate_overhead(rows, **kw),
    }


def _pct(est: Mapping[str, Any]) -> str:
    if est.get("value") is None:
        return "n/a"
    ci = "no CI" if est.get("lo") is None else f"{est['lo'] * 100:.1f}-{est['hi'] * 100:.1f}"
    return f"{est['value'] * 100:.1f}% [{ci}]"


def _usd(est: Mapping[str, Any]) -> str:
    if est.get("value") is None:
        return "n/a"
    ci = "no CI" if est.get("lo") is None else f"{est['lo']:.4f}-{est['hi']:.4f}"
    return f"${est['value']:.4f} [{ci}]"


def _row(cells: Sequence[Any]) -> str:
    return "| " + " | ".join(str(c) for c in cells) + " |"


def render_markdown(rep: Mapping[str, Any]) -> str:
    p = rep["params"]
    spend = "unknown" if rep["spend_usd"] is None else format(rep["spend_usd"], ".4f")
    sources = ", ".join("{} ({} records)".format(s["dir"], s["records"]) for s in rep["sources"])
    lines = ["# Tripwire results report", ""]
    if rep["pilot"]:
        lines += ["> **PILOT RUN.** Not the scored run; do not quote these numbers as results.", ""]
    if rep["incomplete"]:
        halted = ", ".join(rep["incomplete"])
        lines += [f"> **INCOMPLETE:** run halted in {halted}.", ""]
    lines += [
        f"Sources: {sources}.",
        f"CIs: {p['bootstrap']}, {p['resamples']} resamples, seed {p['seed']}, "
        f"{p['ci'] * 100:.0f}%. Each rate is a pair: **strict floor** (excluded runs count as "
        "incorrect) / **published** (excluded runs dropped). Brackets are CIs, in points.",
        f"Records: {rep['records']}. Exclusions: {rep['exclusions_by_code'] or 'none'}. "
        f"Spend in these records: {spend} USD.",
        "",
        "## Fault to correct-outcome (higher is better)",
        "",
        _row(
            ["condition", "fault", "runs", "excluded", "strict floor", "published"]
            + ["$/correct (strict)", "$/correct (published)"]
        ),
        _row(["---"] * 8),
    ]
    for c in rep["cells"]:
        if c["fault"] != "clean":
            lines.append(
                _row(
                    [c["condition"], c["fault"], c["runs"], c["excluded"] or "-"]
                    + [_pct(c["strict"]), _pct(c["published"])]
                    + [_usd(c["strict_cost_per_correct"]), _usd(c["published_cost_per_correct"])]
                )
            )
    lines += [
        "",
        "## Clean-run false-flag rate (lower is better)",
        "",
        _row(["condition", "clean runs", "excluded", "strict (worst case)", "published"]),
        _row(["---"] * 5),
    ]
    lines += [
        _row(
            [c["condition"], c["runs"], c["excluded"] or "-", _pct(c["strict"])]
            + [_pct(c["published"])]
        )
        for c in rep["cells"]
        if c["fault"] == "clean"
    ]
    go = rep["gate_overhead"]
    if go:
        lines += ["", "## Gate overhead", ""]
        if go["cost_overhead"] is None:
            lines.append(
                f"No gate run has a priced `{go['comparator']}` twin ({go['gate_runs']} gate runs)."
            )
        else:
            lines.append(
                f"Cost over `{go['comparator']}` on {go['pairs']} paired runs: "
                f"{_pct(go['cost_overhead'])}; extra tool calls per run "
                f"{go['extra_tool_calls_per_run']:.2f}; added latency "
                f"{go['added_latency_s_per_run']:.3f} s per run; added tokens {go['added_tokens']}."
            )
    return "\n".join(lines) + "\n"


def write_report(rep: Mapping[str, Any], out_dir: Path, name: str) -> tuple[Path, Path]:
    """Write ``<name>.md`` and ``<name>.json``; an existing report is never overwritten."""
    md, js = out_dir / f"{name}.md", out_dir / f"{name}.json"
    if md.exists() or js.exists():
        raise ReportError(
            f"{name}: report exists; pick another --name (reports are not overwritten)"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    md.write_text(render_markdown(rep))
    js.write_text(json.dumps(rep, indent=2, sort_keys=True) + "\n")
    return md, js


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tripwire.report", description=__doc__.splitlines()[0])
    ap.add_argument("run_dirs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=Path("reports"))
    ap.add_argument("--name", help="report file stem (default: run directory names)")
    ap.add_argument("--resamples", type=int, default=BOOTSTRAP_RESAMPLES)
    ap.add_argument("--seed", type=int, default=BOOTSTRAP_SEED)
    args = ap.parse_args(argv)
    try:
        if args.resamples < 100:
            raise ReportError("--resamples must be at least 100")
        rows, sources = load_records(args.run_dirs)
        rep = analyse(rows, sources, resamples=args.resamples, seed=args.seed)
        md, js = write_report(rep, args.out, args.name or "+".join(d.name for d in args.run_dirs))
    except ReportError as exc:
        print(f"report refused: {exc}")
        return 2
    print(f"wrote {md} and {js}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
