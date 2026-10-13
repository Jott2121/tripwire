"""Offline gate replay (issue #44): re-run the improved gate over recorded runs, ZERO live spend.

    python -m tripwire.gate_replay <prior_run_dir> <gate_run_dir> [--out reports --name issue44]

The improved :class:`~tripwire.gate.Gate` adds a store cross-check that catches F1/F3 and a
downgrade of an agent's unsupported value refusal (see ``tripwire/gate.py``). This module replays
that gate over the data already recorded for the current gate run and re-adjudicates each run, so
:mod:`tripwire.report` can quote before/after numbers without any new xAI call. It reads only the
run directories' ``receipt.jsonl`` / ``meta.json`` / ``result.json`` and the per-run injection
ledgers; it never calls a model and never writes to ``runs/spend_ledger.jsonl``.

What is and is not recomputable offline (the receipt records tool-call *names*, not args or served
outputs, so a literal re-run of ``Gate.observe`` over the served bytes is impossible):

* **Faulted runs** are recomputed from the injection ledger, which records, for every fault that
  fired, the trusted ``original`` and the ``served`` value. The new cross-check fires exactly when
  a served quantity disagrees with the store, i.e. on an F1/F3 injection; the fault is classified
  F1/F3 from the served/original ratio against the defined unit factors in ``standards`` (the same
  decision :func:`tripwire.gate.classify_value_fault` makes from units, proven equivalent in the
  tests). F2/F4/F5/F6 the gate already caught; those runs are unchanged.
* **Clean runs** are reconstructed exactly: a clean run serves the store byte for byte, so the
  improved gate is run over results rebuilt from the store for the tools the agent actually called.
  It never blocks (the data matches the store), but it records the positive quantity evidence that
  lets :meth:`Gate.clear_unsupported` downgrade an unsupported refusal.

Exclusions from the receipt (``RUN_ERROR``, ``INJECTION_UNVERIFIED``) are preserved unchanged; the
gate tweak cannot change whether a planned fault fired. ``BASELINE_FAIL`` is still applied by
:mod:`tripwire.report` at analysis over the replayed rows.
"""

from __future__ import annotations

import argparse
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from tripwire import contract as c
from tripwire import standards as s
from tripwire import tasks as tk
from tripwire.adjudicator import FLAG_VOCABULARY, Status, Verdict, adjudicate
from tripwire.faults import FAULT_NAMES, FaultKind
from tripwire.gate import Gate, downgrade_clean
from tripwire.harness import _no_verdict, _verdict_dict
from tripwire.ledger import LedgerError, read_ledger
from tripwire.report import analyse, load_records, write_report
from tripwire.store import Store

QUANTITY_READ_TOOLS = ("get_spec", "get_test_result", "query_defects")
# Quantity fields the gate cross-checks; an F1/F3 ledger event on one of these is a value mismatch.
QUANTITY_FIELD_NAMES = (
    "minimum",
    "maximum",
    "measured",
    "crack_length",
    "aircraft_total_time",
    "aircraft_total_cycles",
)
_REL_TOL = 1e-6  # loose: the swap factors (>=4.448 or <=0.2248) are far from F3's 0.8..1.25
GATE = "gate"


class ReplayError(ValueError):
    """The recorded runs cannot be replayed (fail closed: no numbers are produced)."""


def _swap_ratios() -> tuple[float, ...]:
    """Every defined unit-swap ratio and its inverse, from ``standards`` (not fault design)."""
    out: list[float] = []
    for factor in s.CONVERSION_FACTORS.values():
        out += [factor, 1.0 / factor]
    return tuple(out)


_SWAP_RATIOS = _swap_ratios()


def ratio_fault(original: Any, served: Any) -> FaultKind | None:
    """Classify a ledger value change the way the gate's store cross-check would.

    ``None`` if the values agree, ``F1`` if the served/original ratio is a defined unit-swap
    factor (the served number is the trusted value in a swapped unit), else ``F3``.
    """
    if isinstance(original, bool) or isinstance(served, bool):
        return None
    if not isinstance(original, int | float) or not isinstance(served, int | float):
        return None
    if math.isclose(served, original, rel_tol=1e-9, abs_tol=1e-12):
        return None
    if original == 0:
        return FaultKind.F3  # no ratio; a changed value with no unit-swap signature is wrong_value
    ratio = served / original
    if any(math.isclose(ratio, f, rel_tol=_REL_TOL) for f in _SWAP_RATIOS):
        return FaultKind.F1
    return FaultKind.F3


def slot(run_id: str) -> str:
    """The task/replicate slot a job id names, e.g. ``T05-r03`` from a full gate job id."""
    return run_id.split(".", 1)[0]


def _ledger_events(ledger_dir: Path, run_id: str) -> tuple[list[Any], Any]:
    path = ledger_dir / f"{run_id}.jsonl"
    if not path.is_file():
        raise ReplayError(f"no injection ledger for {run_id} at {path}")
    try:
        return read_ledger(path)
    except LedgerError as exc:
        raise ReplayError(f"{run_id}: {exc}") from exc


def _new_crosscheck_flags(events: Iterable[Any]) -> set[str]:
    """Flags the new store cross-check raises for a faulted run, from its ledger events."""
    flags: set[str] = set()
    for e in events:
        if e.fault not in (FaultKind.F1.value, FaultKind.F3.value):
            continue
        if not any(f".{name}." in f"{e.field}." for name in QUANTITY_FIELD_NAMES):
            continue
        kind = ratio_fault(e.original, e.served)
        if kind is not None:
            flags.add(FAULT_NAMES[kind])
    return flags


def _coerce_query_args(args: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(args)
    if out.get("since"):
        out["since"] = date.fromisoformat(out["since"])
    elif "since" in out:
        del out["since"]
    return {k: v for k, v in out.items() if v not in (None, "")}


def _clean_result(store: Store, call: tk.ToolCall) -> dict[str, Any] | None:
    """The clean server output for one task call, rebuilt from the store (clean == store)."""
    tool, args = call.tool, call.args
    if tool == "get_spec":
        spec = store.get_spec(args["spec_id"], args["parameter"], args["applies_to"],
                              args.get("revision"))  # fmt: skip
        return {"spec": c.to_jsonable(spec)}
    if tool == "get_test_result":
        return {"test_result": c.to_jsonable(store.get_test_result(args["test_id"]))}
    if tool == "query_defects":
        page = store.query_defects(**_coerce_query_args(args))
        return {
            "defects": [c.to_jsonable(d) for d in page.defects],
            "total": page.total,
            "truncated": page.truncated,
        }
    return None


def _clean_gate(store: Store, task: tk.Task, tool_calls: Sequence[str]) -> Gate:
    """Run the improved gate over a clean run's results, rebuilt for the tools the agent called."""
    gate = Gate(store)
    called = set(tool_calls)
    for call in task.calls:
        if call.tool in QUANTITY_READ_TOOLS and call.tool in called:
            gate.observe(call.tool, call.args, _clean_result(store, call) or {})
    return gate


def replay_gate_row(
    row: dict[str, Any], store: Store, tasks_by_id: Mapping[str, tk.Task], ledger_dir: Path
) -> dict[str, Any]:
    """One replayed gate receipt row. Non-gate or excluded rows are returned unchanged."""
    if row["condition"] != GATE or row["exclusion"] is not None:
        return row
    run_id, task = row["run_id"], tasks_by_id[row["task_id"]]
    gate_dict = dict(row.get("gate") or {})
    old_failures = list(gate_dict.get("failures", []))
    old_flags = {FAULT_NAMES[FaultKind(f["fault"])] for f in old_failures if f["fault"]}

    if row["planned_fault"] is not None:
        events, head = _ledger_events(ledger_dir, run_id)
        new_flags = _new_crosscheck_flags(events)
        quantity_checks = len({e.field for e in events if e.fault in ("F1", "F3")})
        downgraded = False
    else:  # clean run: reconstruct the gate over store-truth results (clean serves the store)
        events, head = _ledger_events(ledger_dir, run_id)
        clean = _clean_gate(store, task, row["tool_calls"])
        new_flags, quantity_checks, downgraded = set(), clean.quantity_checks, False
        if clean.blocked:  # a clean run can never disagree with the store
            raise ReplayError(f"{run_id}: clean replay blocked unexpectedly")

    blocked = bool(old_failures) or bool(new_flags)
    if blocked:
        flags = tuple(sorted(old_flags | new_flags, key=FLAG_VOCABULARY.index))
        verdict: Verdict | None = Verdict(Status.BLOCKED, None, None, None, flags)
    else:
        model = Verdict.from_dict(row["verdict"]) if row["verdict"] is not None else None
        if model is not None:
            model, downgraded = downgrade_clean(
                model, blocked=False, quantity_checks=quantity_checks
            )
        verdict = model

    # Re-adjudicate only when the gate actually changed the verdict. An unchanged verdict keeps
    # its recorded adjudication, which is exact and avoids re-deriving work-order truth (the order
    # was written to that run's store, which this offline replay does not rebuild).
    new_vd = _verdict_dict(verdict)
    if new_vd == row["verdict"]:
        adj_dict = row["adjudication"]
    elif verdict is None:
        adj_dict = asdict(_no_verdict(run_id, tuple(e.fault for e in events)))
    else:
        adj_dict = asdict(adjudicate(events, head, store, task, verdict, run_id))

    value_detail = [{"check": "value_mismatch", "fault": _fault_of(f), "detail": "replayed"}
                    for f in sorted(new_flags, key=FLAG_VOCABULARY.index)]  # fmt: skip
    gate_dict.update(
        blocked=blocked,
        failures=old_failures + value_detail,
        quantity_checks=quantity_checks,
        downgraded=downgraded,
        agent_verdict=row["verdict"] if (blocked or downgraded) else None,
    )
    return {**row, "verdict": new_vd, "adjudication": adj_dict, "gate": gate_dict}


def _fault_of(flag: str) -> str:
    return next(k.value for k, name in FAULT_NAMES.items() if name == flag)


def _filter_to_slots(rows: Iterable[dict[str, Any]], slots: set[str]) -> list[dict[str, Any]]:
    return [r for r in rows if slot(r["run_id"]) in slots]


def replay(
    prior_dir: Path, gate_dir: Path, *, fold: str = "all"
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (before_rows, after_rows, sources): the comparator conditions filtered to the gate
    slots, plus the gate rows as recorded (before) and replayed (after). ``fold`` keeps every task
    (``all``), the odd-indexed tasks (``A``) or the even-indexed tasks (``B``)."""
    store, tasks = tk.default_store(), tk.default_tasks()
    tasks_by_id = {t.task_id: t for t in tasks}
    ledger_dir = gate_dir / "ledgers"
    prior_rows, prior_src = load_records([prior_dir])
    gate_rows, gate_src = load_records([gate_dir])
    gate_slots = {slot(r["run_id"]) for r in gate_rows if r["condition"] == GATE}
    keep = _fold_tasks(tasks, fold)
    comparator = [r for r in _filter_to_slots(prior_rows, gate_slots) if r["task_id"] in keep]
    gate_recorded = [r for r in gate_rows if r["condition"] == GATE and r["task_id"] in keep]
    replayed = [replay_gate_row(r, store, tasks_by_id, ledger_dir) for r in gate_recorded]
    return comparator + gate_recorded, comparator + replayed, prior_src + gate_src


def _fold_tasks(tasks: Sequence[tk.Task], fold: str) -> set[str]:
    """Held-out split by task index (fixed, outcome-independent): A = odd tasks, B = even."""
    if fold == "all":
        return {t.task_id for t in tasks}
    if fold not in ("A", "B"):
        raise ReplayError(f"fold must be all|A|B, got {fold!r}")
    want = 0 if fold == "A" else 1
    return {t.task_id for i, t in enumerate(tasks) if i % 2 == want}


def gate_cells(rows: Sequence[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    """The report cells for the gate condition only, keyed by fault, for a compact comparison."""
    rep = analyse(rows, [], **kw)
    out: dict[str, Any] = {}
    for cell in rep["cells"]:
        if cell["condition"] == GATE:
            out[cell["fault"]] = cell
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tripwire.gate_replay", description=__doc__.splitlines()[0])
    ap.add_argument("prior_dir", type=Path, help="baseline+prompt_fix run dir")
    ap.add_argument("gate_dir", type=Path, help="gate run dir")
    ap.add_argument("--out", type=Path, default=Path("reports"))
    ap.add_argument("--name", default="issue44_gate_after")
    ap.add_argument("--resamples", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)
    _, after, sources = replay(args.prior_dir, args.gate_dir)
    rep = analyse(after, sources, resamples=args.resamples, seed=args.seed)
    md, js = write_report(rep, args.out, args.name)
    print(f"wrote {md} and {js}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
