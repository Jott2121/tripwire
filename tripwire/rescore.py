"""Offline re-scoring of recorded receipts under amendment A3 (zero live spend).

A3: when a gated run hits the turn cap after the gate has blocked a fault, the gate's fail-closed
blocked report is the run's verdict (``tripwire.harness.run_one`` does this for new runs). This
module applies the same rule to receipts recorded before A3. It changes only gate rows with
``turn_cap_hit``, no exclusion, no verdict and ``gate.blocked``; every other row is returned
unchanged. Baseline and prompt_fix rows have no gate, so no blocked report exists and they are
never changed (the rule is symmetric: each condition is scored on what the system itself emitted).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from tripwire import tasks as tk
from tripwire.adjudicator import FLAG_VOCABULARY, Status, Verdict, adjudicate
from tripwire.faults import FAULT_NAMES, FaultKind
from tripwire.harness import _verdict_dict
from tripwire.ledger import read_ledger

GATE = "gate"


def a3_applies(row: dict[str, Any]) -> bool:
    gate = row.get("gate") or {}
    return (
        row["condition"] == GATE
        and bool(row.get("turn_cap_hit"))
        and row["exclusion"] is None
        and row["verdict"] is None
        and bool(gate.get("blocked"))
    )


def blocked_verdict(gate: dict[str, Any]) -> Verdict:
    """The verdict ``Gate.report`` emits for these recorded failures."""
    names = {FAULT_NAMES[FaultKind(f["fault"])] for f in gate.get("failures", []) if f["fault"]}
    flags = tuple(sorted(names, key=FLAG_VOCABULARY.index))
    return Verdict(Status.BLOCKED, None, None, None, flags)


def rescore_row(row: dict[str, Any], ledger_dir: Path, tasks_by_id: dict[str, tk.Task]):
    if not a3_applies(row):
        return row
    events, head = read_ledger(ledger_dir / f"{row['run_id']}.jsonl")
    verdict = blocked_verdict(row["gate"])
    task = tasks_by_id[row["task_id"]]
    adj = adjudicate(events, head, tk.default_store(), task, verdict, row["run_id"])
    gate = {**row["gate"], "a3_turn_cap_report": True}
    return {**row, "verdict": _verdict_dict(verdict), "adjudication": asdict(adj), "gate": gate}


def rescore(rows: Iterable[dict[str, Any]], ledger_dir: Path) -> list[dict[str, Any]]:
    tasks_by_id = {t.task_id: t for t in tk.default_tasks()}
    return [rescore_row(r, ledger_dir, tasks_by_id) for r in rows]
