"""v2 report (preregistration v2, change 7): per-model tables, C1-C6, Holm, a verdict per model.

``python -m tripwire.report_v2 --run grok-4.7=out/s1,out/s2 --run gpt-oss-120b=out/m4 --out DIR``

Reads only scored v2 run directories (``tripwire.live_v2 ... --arm scored``; a pilot or a bridge
directory is refused) and their per-run injection ledgers. No model call, no spend.

* **Arms.** Each live record carries three adjudications of one trajectory: the agent's own and
  one per shadow gate (``contract``, ``crosscheck``). So per model there are six arms: baseline,
  baseline+contract, baseline+crosscheck, prompt_fix, prompt_fix+contract, prompt_fix+crosscheck.
* **Coverage class** of each faulted slot (``contract``, ``store``, ``blind``) is fixed by the plan
  and checked against every event of the run's injection ledger: a disagreement, a fault that
  fired other than the planned one, or a missing ledger on a run that was not excluded fails
  closed.
* **Strict floors** are primary: an excluded run counts as incorrect, and on clean runs as a false
  flag. The published number (v1's exclusions, including ``BASELINE_FAIL``) is reported beside
  each strict rate, never alone.
* **CIs** are task-cluster bootstrap percentile intervals (``report.bootstrap_ci``'s sampler,
  10,000 resamples, seed 1). Paired differences are per slot: the shadow arms share one record
  with their live arm; baseline and prompt_fix share the slot.
* **Holm** across the preregistered family of four models for C1, with two-sided bootstrap
  p-values. A model with no records is in the family with p = 1 (never rejected), so running
  fewer models never makes the test easier.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from tripwire import clients, tasks_v2
from tripwire.faults import FaultKind
from tripwire.harness import SHADOW_VARIANTS
from tripwire.ledger import LedgerError, read_ledger, verify_ledger
from tripwire.report import (
    ALPHA,
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    ReportError,
    effective_exclusions,
    load_records,
)

FAMILY = tuple(row.config.label for row in clients.V2_MODELS)  # the four preregistered models
EXPECTED_RECORDS = 960
CLASSES = ("contract", "store", "blind")
CATCHABLE = ("contract", "store")
LIVE = ("baseline", "prompt_fix")
ARMS = tuple(f"{c}+{v}" if v else c for c in LIVE for v in ("", *SHADOW_VARIANTS))
C1_MIN_LIFT = 0.10
C2_FAIL_BELOW = 0.90
C5_BAND = 0.05
C6_MAX_GATE_FF = 0.05
EPS = 1e-9  # thresholds are inclusive; absorb float rounding only
_STORE = frozenset({FaultKind.F1, FaultKind.F3, FaultKind.F7, FaultKind.F8})
_PLAN = {r.run_id: r for r in tasks_v2.default_plan()}

Clusters = Mapping[str, Sequence[float]]


def ledger_class(fault: FaultKind, tool: str, field: str) -> str:
    """The preregistered class of one injection, from its ledger record only."""
    if fault in _STORE:
        return "store"
    if fault is FaultKind.F10 or (fault is FaultKind.F5 and tool == "query_defects"
                                  and field != "defects"):  # fmt: skip
        return "blind"
    return "contract"


def planned_class(fault: FaultKind) -> str:
    """The class the frozen plan gives a fault (it has no F5 text cuts; the ledger checks it)."""
    return ledger_class(fault, "", "defects")


def slot_of(run_id: str) -> str:
    slot = run_id.split(".", 1)[0]
    if slot not in _PLAN:
        raise ReportError(f"{run_id}: not a v2 slot")
    return slot


# --- loading ----------------------------------------------------------------------------------


def load_model(dirs: Sequence[Path], label: str) -> tuple[list[dict[str, Any]], list[Any]]:
    """Scored v2 records of one model from its shard directories, each slot's class verified."""
    rows, sources = load_records(dirs)  # fails closed on duplicates and malformed rows
    for s in sources:
        if s["meta"].get("study") != "v2" or s["meta"].get("arm") != "scored":
            raise ReportError(f"{s['dir']}: not a scored v2 run directory")
    where = {}
    for d in dirs:
        for line in (d / "receipt.jsonl").read_text().splitlines():
            if line.strip():
                where[json.loads(line)["run_id"]] = d
    for r in rows:
        if r["model_label"] != label or r.get("task_set") != "v2" or r["condition"] not in LIVE:
            raise ReportError(f"{r['run_id']}: not a {label} v2 live-arm record")
        plan = _PLAN[slot_of(r["run_id"])]
        if (plan.fault.value if plan.fault else None) != r["planned_fault"]:
            raise ReportError(f"{r['run_id']}: planned fault differs from the frozen plan")
        r["coverage"] = _verified_class(r, plan.fault, where[r["run_id"]] / "ledgers")
        if r["exclusion"] is None and not all(
            v in (r.get("shadow") or {}) for v in SHADOW_VARIANTS
        ):
            raise ReportError(f"{r['run_id']}: a scored record without both shadow gates")
    return rows, sources


def _verified_class(r: Mapping[str, Any], fault: FaultKind | None, ledgers: Path) -> str | None:
    if fault is None:
        return None
    klass = planned_class(fault)
    path = ledgers / f"{r['run_id']}.jsonl"
    if not path.exists():
        if r["exclusion"] is None:
            raise ReportError(f"{r['run_id']}: faulted run with no injection ledger")
        return klass
    try:
        events, head = read_ledger(path)
    except LedgerError as exc:
        raise ReportError(f"{r['run_id']}: {exc}") from exc
    if not verify_ledger(events, head):
        raise ReportError(f"{r['run_id']}: injection ledger fails verification")
    for e in events:
        if e.fault != fault.value or ledger_class(fault, e.tool, e.field) != klass:
            raise ReportError(f"{r['run_id']}: ledger shows {e.fault} in another class")
    return klass


# --- statistics -------------------------------------------------------------------------------


def _outcome(r: Mapping[str, Any], variant: str | None, key: str) -> bool:
    adj = r["adjudication"] if variant is None else r["shadow"][variant]["adjudication"]
    return bool(adj and adj[key])


def boot(clusters: Clusters, resamples: int, seed: int = BOOTSTRAP_SEED) -> list[float]:
    """Sorted resampled means over whole task clusters (``report.bootstrap_ci``'s sampler)."""
    totals = [(sum(v), len(v)) for _, v in sorted(clusters.items())]
    k, rng, stats = len(totals), random.Random(seed), []
    if k < 2:
        return []
    for _ in range(resamples):
        picks = [totals[rng.randrange(k)] for _ in range(k)]
        den = sum(d for _, d in picks)
        if den:
            stats.append(sum(n for n, _ in picks) / den)
    return sorted(stats)


def _pct(stats: Sequence[float], q: float) -> float | None:
    return stats[round(q * (len(stats) - 1))] if stats else None


def estimate(clusters: Clusters, resamples: int, alpha: float = ALPHA) -> dict[str, Any]:
    n = sum(len(v) for v in clusters.values())
    stats = boot(clusters, resamples)
    return {
        "value": sum(sum(v) for v in clusters.values()) / n if n else None,
        "n": n,
        "lo": stats[int(alpha / 2 * (len(stats) - 1))] if stats else None,
        "hi": _pct(stats, 1 - alpha / 2),
        "p_two_sided": min(
            1.0, 2 * min(sum(s <= 0 for s in stats), sum(s >= 0 for s in stats)) / len(stats)
        )
        if stats
        else 1.0,  # fmt: skip
        "_stats": stats,
    }


def _arm(arm: str) -> tuple[str, str | None]:
    live, _, variant = arm.partition("+")
    return live, variant or None


Row = Mapping[str, Any]
Keep = Callable[[Row], bool]


def _rate_clusters(
    rows: Sequence[Row],
    arm: str,
    keep: Keep,
    metric: str = "correct",
    published: Mapping[str, str | None] | None = None,
) -> dict[str, list[float]]:
    """Per-task outcomes of one arm. Strict (``published`` None): every run counts, an excluded
    run as incorrect (or, for false flags, as a flag). Published: excluded runs are dropped."""
    live, variant = _arm(arm)
    out: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r["condition"] != live or not keep(r):
            continue
        code = r["exclusion"] if published is None else published[r["run_id"]]
        if published is not None and code is not None:
            continue
        if metric == "correct":
            out[r["task_id"]].append(float(code is None and _outcome(r, variant, "correct")))
        else:
            out[r["task_id"]].append(float(code is not None or _outcome(r, variant, "false_flag")))
    return out


def _strict_correct(r: Row, arm: str) -> float:
    return float(r["exclusion"] is None and _outcome(r, _arm(arm)[1], "correct"))


def paired(rows: Sequence[Row], arm_a: str, arm_b: str, keep: Keep) -> dict[str, list[float]]:
    """Per-slot strict correct(a) - correct(b), clustered by task; a slot missing an arm drops."""
    by_slot = {(slot_of(r["run_id"]), r["condition"]): r for r in rows}
    out: dict[str, list[float]] = defaultdict(list)
    for slot in sorted({s for s, _ in by_slot}):
        a, b = by_slot.get((slot, _arm(arm_a)[0])), by_slot.get((slot, _arm(arm_b)[0]))
        if a is not None and b is not None and keep(a):
            out[a["task_id"]].append(_strict_correct(a, arm_a) - _strict_correct(b, arm_b))
    return out


def _public(est: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in est.items() if not k.startswith("_")}


def _faulted(r: Row) -> bool:
    return r["planned_fault"] is not None


def _clean(r: Row) -> bool:
    return r["planned_fault"] is None


def _in(*classes: str) -> Keep:
    return lambda r: r["coverage"] in classes


def _of_fault(fault: FaultKind) -> Keep:
    return lambda r: r["planned_fault"] == fault.value


def analyse_model(
    rows: Sequence[dict[str, Any]], sources: Sequence[Any], resamples: int
) -> dict[str, Any]:
    """Arm x class cells (strict and published), per-fault points, and the C1-C6 estimates."""
    excl = effective_exclusions(rows)
    groups = {**{k: _in(k) for k in CLASSES}, "catchable": _in(*CATCHABLE)}
    cells: list[dict[str, Any]] = []
    for arm in ARMS:
        for name, keep in groups.items():
            strict = estimate(_rate_clusters(rows, arm, keep), resamples)
            pub = estimate(_rate_clusters(rows, arm, keep, published=excl), resamples)
            cells.append({"arm": arm, "class": name, "strict": _public(strict),
                          "published": _public(pub)})  # fmt: skip
        ff = estimate(_rate_clusters(rows, arm, _clean, "false_flag"), resamples)
        cells.append({"arm": arm, "class": "clean_false_flag", "strict": _public(ff)})
    per_fault = {
        arm: {f.value: _point(_rate_clusters(rows, arm, _of_fault(f))) for f in FaultKind}
        for arm in ARMS
    }
    gate, pf = "prompt_fix+crosscheck", "prompt_fix"
    tests = {
        "C1": estimate(paired(rows, gate, pf, _in(*CATCHABLE)), resamples),
        "C2": estimate(_rate_clusters(rows, "baseline", _in(*CATCHABLE)), resamples),
        "C3": estimate(paired(rows, pf, "baseline", _faulted), resamples),
        "C4": estimate(paired(rows, gate, "prompt_fix+contract", _in("store")), resamples),
        "C5": estimate(paired(rows, gate, pf, _in("blind")), resamples),
        "C6_gate": estimate(_rate_clusters(rows, gate, _clean, "false_flag"), resamples),
        "C6_prompt_fix": estimate(_rate_clusters(rows, pf, _clean, "false_flag"), resamples),
    }
    done = ("OK", "RUN_ERRORS")
    slots = Counter(r["coverage"] or "clean" for r in rows if r["condition"] == "baseline")
    return {
        "records": len(rows),
        "incomplete": [s["dir"] for s in sources if s["result"].get("exit_name") not in done],
        "coverage_slots": dict(sorted(slots.items())),
        "exclusions": dict(sorted(Counter(c for c in excl.values() if c).items())),
        "cells": cells,
        "per_fault_strict": per_fault,
        "_tests": tests,
    }


def _point(clusters: Clusters) -> float | None:
    n = sum(len(v) for v in clusters.values())
    return sum(sum(v) for v in clusters.values()) / n if n else None


def binomial_upper(k: int, n: int, alpha: float = ALPHA) -> float:
    """Exact one-sided upper bound (Clopper-Pearson) on a rate with ``k`` of ``n``.

    C6 uses the larger of this and the bootstrap's upper bound: with no false flag the bootstrap
    interval collapses to [0, 0], which would make the 5% rule free; the exact bound (about 3.7%
    at 0 of 80, as planned in the preregistration) does not."""
    if n < 1 or not 0 <= k <= n:
        raise ValueError(f"need 0 <= k <= n and n >= 1, got {k}/{n}")
    if k == n:
        return 1.0

    def cdf(p: float) -> float:  # P(X <= k) for X ~ Binomial(n, p)
        return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k + 1))

    lo, hi = k / n, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if cdf(mid) > alpha else (lo, mid)
    return hi


def holm(p_values: Mapping[str, float], alpha: float = ALPHA) -> dict[str, tuple[bool, float]]:
    """Holm step-down over the family: model -> (rejected, the alpha it was tested at)."""
    out, going = {}, True
    for j, (m, p) in enumerate(sorted(p_values.items(), key=lambda kv: (kv[1], kv[0]))):
        level = alpha / (len(p_values) - j)
        going = going and p <= level
        out[m] = (going, level)
    return out


def verdicts(models: Mapping[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Apply C1-C6 and the headline rule to each model in the family. Never relaxes a rule."""
    ps = {m: (models[m]["_tests"]["C1"]["p_two_sided"] if m in models else 1.0) for m in FAMILY}
    adjusted = holm(ps)
    out = {}
    for m in FAMILY:
        if m not in models:
            out[m] = {"verdict": "INCONCLUSIVE", "reason": "no records", "checks": {}}
            continue
        rep, t = models[m], models[m]["_tests"]
        rejected, level = adjusted[m]
        c1 = t["C1"]
        lo_adj = c1["_stats"][int(level / 2 * (len(c1["_stats"]) - 1))] if c1["_stats"] else None
        gate_ff, pf_ff = t["C6_gate"], t["C6_prompt_fix"]
        c6_upper = None
        if gate_ff["value"] is not None and gate_ff["hi"] is not None:
            flags = round(gate_ff["value"] * gate_ff["n"])
            c6_upper = max(gate_ff["hi"], binomial_upper(flags, gate_ff["n"]))
        checks = {
            "C1": c1["value"] is not None
            and c1["value"] >= C1_MIN_LIFT - EPS
            and rejected
            and lo_adj is not None
            and lo_adj > 0,
            "C6": gate_ff["value"] is not None
            and pf_ff["value"] is not None
            and gate_ff["value"] <= pf_ff["value"] + EPS
            and c6_upper is not None
            and c6_upper <= C6_MAX_GATE_FF + EPS,
        }
        notes = {
            "C2_upper_below_90": t["C2"]["hi"] is not None and t["C2"]["hi"] < C2_FAIL_BELOW,
            "C3_lift": t["C3"]["lo"] is not None and t["C3"]["lo"] > 0,
            "C5_within_band": t["C5"]["value"] is not None
            and abs(t["C5"]["value"]) <= C5_BAND + EPS,
        }
        complete = rep["records"] >= EXPECTED_RECORDS and not rep["incomplete"]
        verdict = ("GATE GENERALISES" if all(checks.values()) else "DOES NOT GENERALISE") \
            if complete else "INCONCLUSIVE"  # fmt: skip
        out[m] = {
            "verdict": verdict, "complete": complete, "checks": checks, "notes": notes,
            "holm": {"alpha": level, "rejected": rejected, "c1_lower_at_alpha": lo_adj},
            "c6_gate_upper": c6_upper,
            "tests": {k: _public(v) for k, v in t.items()},
        }  # fmt: skip
    return out


def study(models: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    v = verdicts(models)
    c2 = sum(bool(v[m].get("notes", {}).get("C2_upper_below_90")) for m in FAMILY)
    return {
        "family": list(FAMILY),
        "passing": sum(x["verdict"] == "GATE GENERALISES" for x in v.values()),
        "ungated_fail_materially": c2 >= 3,
        "verdicts": v,
        "models": {
            m: {k: x for k, x in rep.items() if not k.startswith("_")} for m, rep in models.items()
        },  # fmt: skip
    }


def _fmt(est: Mapping[str, Any] | None) -> str:
    if not est or est.get("value") is None:
        return "n/a"
    ci = f" [{est['lo'] * 100:.1f}, {est['hi'] * 100:.1f}]" if est.get("lo") is not None else ""
    return f"{est['value'] * 100:.1f}%{ci}"


def render_markdown(rep: Mapping[str, Any]) -> str:
    lines = ["# Tripwire v2 report", "",
             f"Models passing (GATE GENERALISES): **{rep['passing']} of {len(rep['family'])}**.",
             "Strict floors are primary; the published number is beside each. CIs are 95% "
             "task-cluster bootstrap.", ""]  # fmt: skip
    for m in rep["family"]:
        v = rep["verdicts"][m]
        lines += [f"## {m}: {v['verdict']}", ""]
        if m not in rep["models"]:
            lines += ["No records.", ""]
            continue
        t = v["tests"]
        lines += [f"- C1 gate lift, catchable: {_fmt(t['C1'])} (Holm alpha "
                  f"{v['holm']['alpha']:.4f}, pass: {v['checks']['C1']})",
                  f"- C2 baseline correct, catchable: {_fmt(t['C2'])}",
                  f"- C3 prompt lift, all faulted: {_fmt(t['C3'])}",
                  f"- C4 crosscheck minus contract, store class: {_fmt(t['C4'])}",
                  f"- C5 gate lift, gate-blind: {_fmt(t['C5'])}",
                  f"- C6 clean false flags, gate {_fmt(t['C6_gate'])} vs prompt "
                  f"{_fmt(t['C6_prompt_fix'])} (pass: {v['checks']['C6']})", "",
                  "| arm | class | strict | published |", "| --- | --- | --- | --- |"]  # fmt: skip
        for c in rep["models"][m]["cells"]:
            lines.append(f"| {c['arm']} | {c['class']} | {_fmt(c['strict'])} | "
                         f"{_fmt(c.get('published'))} |")  # fmt: skip
        lines.append("")
    return "\n".join(lines)


def write(rep: Mapping[str, Any], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [out_dir / "v2-report.json", out_dir / "v2-report.md"]
    paths[0].write_text(json.dumps(rep, indent=2, sort_keys=True) + "\n")
    paths[1].write_text(render_markdown(rep) + "\n")
    for m, v in rep["verdicts"].items():
        p = out_dir / f"verdict-{m}.json"
        p.write_text(json.dumps(v, indent=2, sort_keys=True) + "\n")
        paths.append(p)
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tripwire.report_v2")
    p.add_argument("--run", action="append", default=[], metavar="LABEL=DIR[,DIR...]")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--resamples", type=int, default=BOOTSTRAP_RESAMPLES)
    args = p.parse_args(argv)
    models = {}
    try:
        for spec in args.run:
            label, _, dirs = spec.partition("=")
            if label not in FAMILY or not dirs or label in models:
                raise ReportError(f"--run needs a distinct v2 model label and dirs: {spec!r}")
            rows, sources = load_model([Path(d) for d in dirs.split(",")], label)
            models[label] = analyse_model(rows, sources, args.resamples)
    except ReportError as exc:
        print(f"REFUSED: {exc}")
        return 2
    for path in write(study(models), args.out):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
