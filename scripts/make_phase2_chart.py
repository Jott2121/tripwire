"""Build the phase two figure from the committed results: noticed vs acted, per model and condition.

Reads ``reports/phase2_results.json`` (the committed summary of the 800 phase two runs) and
writes one deterministic, byte-stable SVG, ``artifacts/phase2_noticed_vs_acted.svg``. For each
model and condition it draws two bars on the 100 mismatched items:

* **flagged** -- the reply carried the ``record_disagreement`` flag (the agent noticed);
* **correct** -- the agent refused or escalated with the flag (the agent acted).

Under each pair it prints the false-alarm rate on the 100 control items. The rules-as-gate
reference is not drawn: it scores 1.0 by construction, because the same rules chose the items.

Every number is read from the JSON; nothing is typed in by hand. Pure standard library, so the
output is identical on every machine. Run it directly or via ``make demo``;
``tests/test_phase2_chart.py`` regenerates it and fails if the committed copy differs.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

RESULTS = Path("reports/phase2_results.json")
OUT = Path("artifacts/phase2_noticed_vs_acted.svg")
MODELS = ("grok-4.7", "muse-spark-open")  # drawn in this order
MODEL_LABELS = {"grok-4.7": "grok-4.7", "muse-spark-open": "Muse Spark Open"}
CONDITIONS = ("baseline", "prompt_fix")
CONDITION_LABELS = {"baseline": "Baseline", "prompt_fix": '"Check your data" prompt'}
# Okabe-Ito, colour-blind safe: sky blue for noticing, vermilion for acting.
FLAGGED_COLOUR = "#56B4E9"
CORRECT_COLOUR = "#D55E00"


def pct(value: float) -> str:
    """A rate in [0, 1] as a whole percent, e.g. ``0.97 -> '97%'``. Fails closed outside [0, 1]."""
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"rate out of range: {value!r}")
    return f"{value * 100:.0f}%"


def extract(results: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One row per (model, condition) with the three rates the figure draws.

    Fails closed (``KeyError``) if a model, condition or rate is missing, and refuses a summary
    whose mismatch or control half is not the 100 items the preregistration froze.
    """
    rows: list[dict[str, Any]] = []
    for model in MODELS:
        conditions = results["models"][model]["conditions"]
        for cond in CONDITIONS:
            summary = conditions[cond]["summary"]
            mined, controls = summary["mined"], summary["controls"]
            if mined["n"] != 100 or controls["n"] != 100:
                raise ValueError(f"{model}/{cond}: expected 100 + 100 items")
            rows.append(
                {
                    "model": model,
                    "condition": cond,
                    "flagged": float(mined["flagged"]),
                    "correct": float(mined["correct"]),
                    "false_alarm": float(controls["false_alarm"]),
                }
            )
    return rows


def build_svg(rows: Sequence[Mapping[str, Any]], runs: int) -> str:
    """A grouped bar chart: flagged vs correct on mismatches, false alarms printed below."""
    width, height = 820, 470
    left, right, top, bottom = 70, 30, 112, 128
    plot_w = width - left - right
    plot_h = height - top - bottom
    base_y = top + plot_h
    slot = plot_w / len(rows)
    bar_w = 46.0
    gap = 6.0

    def y_of(value: float) -> float:
        return top + plot_h * (1 - value)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="system-ui,Helvetica,Arial,sans-serif" '
        'role="img" aria-label="Phase two on real FAA records: share of mismatches each agent '
        'flagged versus share it refused or escalated, by model and condition.">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="{left}" y="34" font-size="20" font-weight="700" fill="#111111">'
        "Real FAA records: the agents noticed the conflict, then answered anyway</text>",
        f'<text x="{left}" y="56" font-size="13" fill="#555555">'
        f"100 report/registry mismatches and 100 clean controls per model and condition, "
        f"{runs} runs, scored by rule</text>",
    ]
    # legend
    legend = (
        (FLAGGED_COLOUR, "Flagged the conflict (noticed)"),
        (CORRECT_COLOUR, "Refused or escalated (acted, scored correct)"),
    )
    lx = left
    for colour, text in legend:
        parts.append(f'<rect x="{lx}" y="70" width="14" height="14" fill="{colour}" rx="2"/>')
        parts.append(
            f'<text x="{lx + 20}" y="82" font-size="13" fill="#333333">{escape(text)}</text>'
        )
        lx += 20 + 8 * len(text) + 24
    # gridlines and ticks
    for i in range(0, 5):
        gv = i / 4
        gy = y_of(gv)
        stroke = "#111111" if i == 0 else "#eeeeee"
        parts.append(
            f'<line x1="{left}" y1="{gy:.1f}" x2="{width - right}" y2="{gy:.1f}" '
            f'stroke="{stroke}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{left - 10}" y="{gy + 4:.1f}" font-size="11" fill="#777777" '
            f'text-anchor="end">{gv * 100:.0f}%</text>'
        )
    # bars, one pair per (model, condition)
    for idx, row in enumerate(rows):
        cx = left + slot * idx + slot / 2
        for offset, key, colour in (
            (-(bar_w + gap / 2), "flagged", FLAGGED_COLOUR),
            (gap / 2, "correct", CORRECT_COLOUR),
        ):
            value = row[key]
            bx = cx + offset
            by = y_of(value)
            parts.append(
                f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" '
                f'height="{base_y - by:.1f}" fill="{colour}" rx="2"/>'
            )
            parts.append(
                f'<text x="{bx + bar_w / 2:.1f}" y="{by - 6:.1f}" font-size="13" '
                f'font-weight="700" fill="#111111" text-anchor="middle">{pct(value)}</text>'
            )
        parts.append(
            f'<text x="{cx:.1f}" y="{base_y + 22:.1f}" font-size="12" fill="#333333" '
            f'text-anchor="middle">{escape(CONDITION_LABELS[row["condition"]])}</text>'
        )
        parts.append(
            f'<text x="{cx:.1f}" y="{base_y + 40:.1f}" font-size="11" fill="#777777" '
            f'text-anchor="middle">false alarms on controls: {pct(row["false_alarm"])}</text>'
        )
    # model labels, centred under their two condition slots
    per_model = len(CONDITIONS)
    for m, model in enumerate(MODELS):
        mx = left + slot * per_model * m + slot * per_model / 2
        parts.append(
            f'<text x="{mx:.1f}" y="{base_y + 68:.1f}" font-size="15" font-weight="700" '
            f'fill="#111111" text-anchor="middle">{escape(MODEL_LABELS[model])}</text>'
        )
    parts.append(
        f'<text x="{left}" y="{height - 30}" font-size="11" fill="#777777">'
        f"Source: {RESULTS.as_posix()}. Flagging the conflict and answering anyway is "
        "scored wrong.</text>"
    )
    parts.append(
        f'<text x="{left}" y="{height - 14}" font-size="11" fill="#777777">'
        "Not drawn: the mining rules as a gate score 100% by construction (they chose the "
        "items), a reference, not a finding.</text>"
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def render(root: Path = Path(".")) -> str:
    """Read the committed results under ``root`` and return the SVG text (does not write)."""
    results = json.loads((root / RESULTS).read_text())
    rows = extract(results)
    runs = sum(results["models"][m]["conditions"][c]["runs"] for m in MODELS for c in CONDITIONS)
    return build_svg(rows, runs)


def write(root: Path = Path(".")) -> Path:
    """Write the SVG under ``<root>/artifacts`` and return its path."""
    path = root / OUT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(root))
    return path


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="make_phase2_chart", description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path("."), help="repo root (default: .)")
    args = ap.parse_args(argv)
    print(f"  wrote {write(args.root)} from {RESULTS.as_posix()} (zero API spend)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
