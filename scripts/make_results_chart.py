"""Build the README results chart and summary from the committed spend ledger.

Reads ``runs/spend_ledger.jsonl`` (the only run data committed to this repo) and writes
two deterministic, byte-stable artifacts under ``artifacts/``:

* ``results_cost.svg`` -- a bar chart of mean USD per run for each condition.
* ``results_summary.json`` -- the aggregates the chart draws, with provenance.

Every figure here is cost. Correctness and false-flag rates come from the adjudicated
report (``python -m tripwire.report``), whose per-run records are produced in CI and are
not committed; this script never touches them and never invents them.

Pure standard library (no plotting dependency), so the output is identical on every
machine and the committed artifacts can be regenerated and diffed. Run it directly or via
``make demo``; ``tests/test_results_chart.py`` exercises the same functions.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

CONDITIONS = ("baseline", "prompt_fix", "gate")  # drawn in this order when present
LEDGER = Path("runs/spend_ledger.jsonl")
ARTIFACTS = Path("artifacts")
# Colour-blind-safe (Okabe-Ito): blue for the neutral baseline, vermilion for the prompt.
BAR_COLOURS = {"baseline": "#0072B2", "prompt_fix": "#D55E00", "gate": "#009E73"}


def load_spend(path: Path) -> list[dict[str, Any]]:
    """Return the ``kind == "spend"`` rows of a ledger file, in file order."""
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if obj.get("kind") == "spend":
            rows.append(obj)
    return rows


def _condition_of(run_id: str) -> str:
    """The experimental condition encoded in a run id like ``T01-r04.grok-4.7.<path>.<cond>.k1``."""
    parts = run_id.split(".")
    for token in parts:
        if token in CONDITIONS:
            return token
    return parts[-2] if len(parts) >= 2 else "unknown"


def _instant(stamp: str) -> float:
    """POSIX seconds for an ISO-8601 stamp, so stamps order by instant not text. Bad stamps last."""
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return float("inf")


def aggregate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Per-condition run count and cost, plus run-wide provenance. Fails closed on no rows."""
    if not rows:
        raise ValueError("spend ledger has no spend rows")
    by_cond: dict[str, list[float]] = defaultdict(list)
    models: set[str] = set()
    paths: set[str] = set()
    stamps: list[str] = []
    for r in rows:
        run_id = str(r["run_id"])
        by_cond[_condition_of(run_id)].append(float(r["cost_usd"]))
        # Layout: <task>.<model>.<path>.<condition>.k<n>; the model label itself contains a
        # dot (grok-4.7), so take model as everything between the task and the trailing three.
        bits = run_id.split(".")
        if len(bits) >= 5:
            models.add(".".join(bits[1:-3]))
            paths.add(bits[-3])
        if r.get("timestamp"):
            stamps.append(str(r["timestamp"]))
    conditions = {
        cond: {
            "runs": len(costs),
            "total_usd": round(sum(costs), 6),
            "mean_usd": round(sum(costs) / len(costs), 6),
        }
        for cond, costs in sorted(by_cond.items())
    }
    total_runs = sum(c["runs"] for c in conditions.values())
    return {
        "source": LEDGER.as_posix(),
        "runs": total_runs,
        "total_usd": round(sum(c["total_usd"] for c in conditions.values()), 6),
        "model": sorted(models)[0] if len(models) == 1 else sorted(models),
        "path": sorted(paths)[0] if len(paths) == 1 else sorted(paths),
        # Order by parsed instant (not lexically) so a differing offset or precision can't
        # mis-window; emit the original string. Unparseable stamps fall back to lexical order.
        "first_timestamp": min(stamps, key=_instant) if stamps else None,
        "last_timestamp": max(stamps, key=_instant) if stamps else None,
        "conditions": conditions,
    }


def _ordered(conditions: Mapping[str, Any]) -> list[str]:
    known = [c for c in CONDITIONS if c in conditions]
    extra = sorted(c for c in conditions if c not in CONDITIONS)
    return known + extra


def build_summary(agg: Mapping[str, Any]) -> str:
    """The committed JSON sidecar (sorted keys, trailing newline) for byte-stable diffs."""
    return json.dumps(agg, indent=2, sort_keys=True) + "\n"


def build_svg(agg: Mapping[str, Any]) -> str:
    """A deterministic bar chart of mean USD per run per condition."""
    conds = _ordered(agg["conditions"])
    means = [agg["conditions"][c]["mean_usd"] for c in conds]
    peak = max(means) if means else 0.0

    width, height = 720, 420
    left, right, top, bottom = 90, 40, 70, 90
    plot_w = width - left - right
    plot_h = height - top - bottom
    n = len(conds)
    slot = plot_w / n if n else plot_w
    bar_w = min(140.0, slot * 0.55)

    def y_of(value: float) -> float:
        return top + plot_h * (1 - (value / peak if peak else 0.0))

    labels = {"baseline": "Baseline", "prompt_fix": '"Check your data" prompt', "gate": "Gate"}
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="system-ui,Helvetica,Arial,sans-serif" '
        'role="img" aria-label="Mean cost per run by condition, from the tripwire spend ledger.">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="{left}" y="34" font-size="20" font-weight="700" fill="#111111">'
        "Cost per run: the safety prompt is not free</text>",
        f'<text x="{left}" y="56" font-size="13" fill="#555555">'
        f"Mean USD per run, grok-4.7, {agg['path']} path "
        f"(n={agg['runs']} runs, source: {agg['source']})</text>",
    ]
    # y axis baseline
    base_y = top + plot_h
    parts.append(
        f'<line x1="{left}" y1="{base_y}" x2="{width - right}" y2="{base_y}" '
        'stroke="#111111" stroke-width="1"/>'
    )
    # gridlines + ticks (four steps)
    for i in range(1, 5):
        gv = peak * i / 4
        gy = y_of(gv)
        parts.append(
            f'<line x1="{left}" y1="{gy:.1f}" x2="{width - right}" y2="{gy:.1f}" '
            'stroke="#eeeeee" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{left - 10}" y="{gy + 4:.1f}" font-size="11" fill="#777777" '
            f'text-anchor="end">${gv:.4f}</text>'
        )
    # bars
    for idx, cond in enumerate(conds):
        mean = agg["conditions"][cond]["mean_usd"]
        runs = agg["conditions"][cond]["runs"]
        cx = left + slot * idx + slot / 2
        bx = cx - bar_w / 2
        by = y_of(mean)
        bh = base_y - by
        colour = BAR_COLOURS.get(cond, "#888888")
        parts.append(
            f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" '
            f'fill="{colour}" rx="3"/>'
        )
        parts.append(
            f'<text x="{cx:.1f}" y="{by - 8:.1f}" font-size="14" font-weight="700" '
            f'fill="#111111" text-anchor="middle">${mean:.4f}</text>'
        )
        parts.append(
            f'<text x="{cx:.1f}" y="{base_y + 24:.1f}" font-size="13" fill="#333333" '
            f'text-anchor="middle">{labels.get(cond, cond)}</text>'
        )
        parts.append(
            f'<text x="{cx:.1f}" y="{base_y + 42:.1f}" font-size="11" fill="#777777" '
            f'text-anchor="middle">{runs} runs</text>'
        )
    parts.append(
        f'<text x="{left}" y="{height - 16}" font-size="11" fill="#777777">'
        "Cost only. Accuracy and false-flag rates come from the adjudicated report, "
        "not this chart.</text>"
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def generate(root: Path = Path(".")) -> dict[str, Any]:
    """Aggregate the ledger and return the summary dict (does not write)."""
    return aggregate(load_spend(root / LEDGER))


def write_artifacts(root: Path = Path(".")) -> tuple[Path, Path, dict[str, Any]]:
    """Write both artifacts under ``<root>/artifacts`` and return their paths and the summary."""
    agg = generate(root)
    out = root / ARTIFACTS
    out.mkdir(parents=True, exist_ok=True)
    svg_path = out / "results_cost.svg"
    json_path = out / "results_summary.json"
    svg_path.write_text(build_svg(agg))
    json_path.write_text(build_summary(agg))
    return svg_path, json_path, agg


def _print_summary(agg: Mapping[str, Any]) -> None:
    print("tripwire offline demo -- replay of recorded spend (zero API spend)")
    print(f"  source : {agg['source']}")
    print(f"  model  : {agg['model']}   path: {agg['path']}")
    print(f"  window : {agg['first_timestamp']} .. {agg['last_timestamp']}")
    print(f"  runs   : {agg['runs']}   total: ${agg['total_usd']:.4f}")
    print("  mean USD per run by condition:")
    for cond in _ordered(agg["conditions"]):
        c = agg["conditions"][cond]
        print(
            f"    {cond:<12} {c['runs']:>4} runs   "
            f"${c['mean_usd']:.6f}/run   ${c['total_usd']:.4f} total"
        )


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="make_results_chart", description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path("."), help="repo root (default: .)")
    args = ap.parse_args(argv)
    svg_path, json_path, agg = write_artifacts(args.root)
    _print_summary(agg)
    print(f"  wrote {svg_path} and {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
