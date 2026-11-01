"""Offline note: the gate's numbers follow from the code, not from the agent. ZERO model spend.

    python -m tripwire.gate_determined [--out docs/results/gate-determined.md]

The store cross-check gate (``Gate(store, crosscheck=True)``) compares every served value with
the same clean store the tools read from. On a run where a value fault fired, it therefore always
disagrees with the store and blocks, naming exactly the fault the ledger recorded; on a clean run
it never blocks, and :func:`tripwire.gate.downgrade_clean` turns an agent's value-disagreement
refusal into ``ok``. So the gate's catch rate and much of its clean-run behaviour are decided
before any model is called. This module shows it by running the real harness, the real fault
injector, the real gates and the real adjudicator over the v1 holdout slots and the full v2 plan,
with four scripted agents that behave as differently as agents can:

* ``oracle``: always answers the true value (the best possible ungated agent);
* ``wrong``: always answers a confident wrong value (the worst);
* ``value_flagger``: always refuses, naming every value-disagreement fault (F1/F3/F7/F8);
* ``contract_flagger``: always refuses, naming a stale spec revision (F2).

If a gate number is the same for all four, a paid run cannot measure it: it is a property of the
gate and the fault design. Nothing here calls a model, reads a key, or touches a spend ledger.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tripwire import holdout, synthdata, tasks_v2
from tripwire import tasks as tk
from tripwire.faults import FAULT_NAMES, FaultKind
from tripwire.harness import run_one
from tripwire.prompts import Condition
from tripwire.report_v2 import planned_class
from tripwire.store import Store
from tripwire.xai import (
    ChatResponse,
    ExecPath,
    ModelConfig,
    Pricing,
    ScriptedClient,
    ToolCallRequest,
)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)  # fixed: the result is reproducible byte for byte
MODEL = ModelConfig("scripted", "no-model", None, "scripted agent, no model is called")
PRICE = Pricing(0.0, 0.0, "no model is called")
VARIANTS = ("contract", "crosscheck")
VALUE_FAULTS = (FaultKind.F1, FaultKind.F3, FaultKind.F7, FaultKind.F8)
POLICIES = ("oracle", "wrong", "value_flagger", "contract_flagger")
DEFAULT_OUT = Path("docs") / "results" / "gate-determined.md"


@dataclass(frozen=True)
class Outcome:
    """One scored run: the ungated agent and each gate variant on the same trajectory."""

    study: str  # "v1-holdout" or "v2-plan"
    policy: str
    fault: str | None
    klass: str  # "clean", or the v2 coverage class ("store", "contract", "blind")
    excluded: bool
    correct: Mapping[str, bool]  # "agent", "contract", "crosscheck" (and "gate" for v1)
    false_flag: Mapping[str, bool]


def _reads(task: tk.Task) -> tuple[ToolCallRequest, ...]:
    """The task's calls that need no earlier result (a work-order read-back needs the new id)."""
    return tuple(
        ToolCallRequest(f"c{i}", c.tool, dict(c.args))
        for i, c in enumerate(task.calls)
        if tk.WO_ID_ARG not in c.args.values()
    )


def _verdict(status: str, value: Any = None, unit: Any = None, wo: Any = None, flags=()) -> str:
    return json.dumps(
        {"status": status, "value": value, "unit": unit, "work_order_id": wo, "flags": list(flags)}
    )


def _truth(task: tk.Task, fresh_store: Callable[[], Store]) -> dict[str, Any]:
    truth = tk.ground_truth(task, fresh_store())
    if task.kind is tk.TaskKind.WORK_ORDER:  # the id a fresh store issues for this one write
        truth["work_order_id"] = (
            fresh_store()
            .create_work_order(task.key[0], task.action, tk.CREATED_BY, NOW)
            .work_order_id
        )
    return truth


def final_answer(policy: str, task: tk.Task, fresh_store: Callable[[], Store]) -> str:
    """The scripted agent's final reply; it never depends on what was served."""
    if policy == "value_flagger":
        return _verdict("refused", flags=[FAULT_NAMES[k] for k in VALUE_FAULTS])
    if policy == "contract_flagger":
        return _verdict("refused", flags=[FAULT_NAMES[FaultKind.F2]])
    truth = _truth(task, fresh_store)
    if policy == "oracle":
        return json.dumps(truth)
    if policy != "wrong":
        raise ValueError(f"unknown policy {policy!r}")
    if task.kind is tk.TaskKind.WORK_ORDER:
        return _verdict("ok", wo="WO-NOT-A-REAL-ORDER")
    value = truth["value"]
    wrong = 1.0 if value is None else value * 2 + 1  # never equal to the truth
    return _verdict("ok", value=wrong, unit=truth["unit"])


def _follow_ups(task: tk.Task, wo_id: str) -> tuple[ToolCallRequest, ...]:
    """The work-order read-back, with the id a fresh store issues (the scripted agent's guess)."""
    return tuple(
        ToolCallRequest(f"c{i}", c.tool, {k: wo_id if v == tk.WO_ID_ARG else v
                                          for k, v in c.args.items()})
        for i, c in enumerate(task.calls)
        if tk.WO_ID_ARG in c.args.values()
    )  # fmt: skip


def _script(task: tk.Task, final: str, fresh_store: Callable[[], Store]) -> list[ChatResponse]:
    def resp(content: str = "", calls=()):
        return ChatResponse("no-model", content, 0, 0, tuple(calls))

    steps = [resp(calls=_reads(task))]
    if task.kind is tk.TaskKind.WORK_ORDER:
        steps.append(resp(calls=_follow_ups(task, _truth(task, fresh_store)["work_order_id"])))
    return [*steps, resp(final)]


def _adj(record: Any, variant: str | None) -> Mapping[str, Any] | None:
    if variant is None:
        return record.adjudication
    return (record.shadow or {}).get(variant, {}).get("adjudication")


def _one(
    study: str,
    policy: str,
    slot: tk.PlannedRun,
    task: tk.Task,
    task_set: str,
    fresh_store: Callable[[], Store],
) -> Outcome:
    final = final_answer(policy, task, fresh_store)
    common = dict(clock=lambda: NOW, pricing=PRICE, task_set=task_set)
    rec = run_one(
        slot, task, MODEL, ExecPath.CLIENT_SIDE, Condition.BASELINE,
        ScriptedClient(_script(task, final, fresh_store)), run_id=f"{slot.run_id}.{policy}",
        shadow_gates=VARIANTS, **common,
    )  # fmt: skip
    adjs: dict[str, Mapping[str, Any] | None] = {"agent": _adj(rec, None)}
    adjs |= {v: _adj(rec, v) for v in VARIANTS}
    if task_set == "v1":  # the live gate condition v1 ran, on the same scripted agent
        live = run_one(
            slot, task, MODEL, ExecPath.CLIENT_SIDE, Condition.GATE,
            ScriptedClient(_script(task, final, fresh_store)),
            run_id=f"{slot.run_id}.{policy}.gate", **common,
        )  # fmt: skip
        adjs["gate"] = _adj(live, None)
    fault = slot.fault
    return Outcome(
        study=study,
        policy=policy,
        fault=fault.value if fault else None,
        klass="clean" if fault is None else planned_class(fault),
        excluded=rec.exclusion is not None,
        correct={k: bool(a and a["correct"]) for k, a in adjs.items()},
        false_flag={k: bool(a and a["false_flag"]) for k, a in adjs.items()},
    )


def v1_slots() -> list[tuple[tk.PlannedRun, tk.Task]]:
    by_id = {t.task_id: t for t in tk.default_tasks()}
    return [(s, by_id[s.task_id]) for s in holdout.select_slots(holdout.holdout_runs())]


def v2_slots() -> list[tuple[tk.PlannedRun, tk.Task]]:
    by_id = {t.task_id: t for t in tasks_v2.default_tasks()}
    return [(s, by_id[s.task_id]) for s in tasks_v2.default_plan()]


def _v1_store() -> Store:
    return Store.from_dataset(synthdata.generate(tk.TASK_DATA_SEED))


def run_all(
    v1: Sequence[tuple[tk.PlannedRun, tk.Task]] | None = None,
    v2: Sequence[tuple[tk.PlannedRun, tk.Task]] | None = None,
    policies: Iterable[str] = POLICIES,
) -> list[Outcome]:
    v1 = v1_slots() if v1 is None else v1
    v2 = v2_slots() if v2 is None else v2
    out: list[Outcome] = []
    for policy in policies:
        out += [_one("v1-holdout", policy, s, t, "v1", _v1_store) for s, t in v1]
        out += [_one("v2-plan", policy, s, t, "v2", tasks_v2.default_store) for s, t in v2]
    return out


def tabulate(outcomes: Sequence[Outcome]) -> dict[str, Any]:
    """Rates per (study, policy, group, arm); groups: each fault, each v2 class, clean."""
    cells: dict[tuple[str, str, str], list[Outcome]] = defaultdict(list)
    for o in outcomes:
        if o.excluded:
            continue
        groups = ["clean"] if o.fault is None else [o.fault, f"class:{o.klass}"]
        for g in groups:
            cells[(o.study, o.policy, g)].append(o)
    table: dict[str, Any] = {}
    for (study, policy, group), rows in sorted(cells.items()):
        metric = "false_flag" if group == "clean" else "correct"
        arms = sorted(rows[0].correct)
        table.setdefault(study, {}).setdefault(policy, {})[group] = {
            "n": len(rows),
            "metric": metric,
            **{a: sum(getattr(r, metric)[a] for r in rows) / len(rows) for a in arms},
        }
    excluded = sum(o.excluded for o in outcomes)
    return {"runs": len(outcomes), "excluded": excluded, "table": table}


def invariant_groups(table: Mapping[str, Any], arm: str) -> dict[str, dict[str, float]]:
    """For each study and group, the arm's value per policy (an invariant one has one value)."""
    out: dict[str, dict[str, float]] = {}
    for study, by_policy in table.items():
        for policy, groups in by_policy.items():
            for group, cell in groups.items():
                if arm in cell:
                    out.setdefault(f"{study} {group}", {})[policy] = cell[arm]
    return out


def checks(table: Mapping[str, Any]) -> dict[str, bool]:
    """The claims the note makes, each computed from the table (never typed in by hand)."""
    faulted = [
        (study, policy, group, cell)
        for study, by_policy in table.items()
        for policy, groups in by_policy.items()
        for group, cell in groups.items()
        if group != "clean" and not group.startswith("class:")
    ]
    catchable = [x for x in faulted if planned_class(FaultKind(x[2])) in ("store", "contract")]
    store = [x for x in faulted if FaultKind(x[2]) in VALUE_FAULTS]
    return {
        "crosscheck_catches_every_catchable_fault_for_every_agent": all(
            x[3]["crosscheck"] == 1.0 for x in catchable
        ),
        "live_gate_equals_crosscheck_shadow_in_v1": all(
            x[3]["gate"] == x[3]["crosscheck"] for x in faulted if "gate" in x[3]
        ),
        "contract_gate_equals_agent_on_value_faults": all(
            x[3]["contract"] == x[3]["agent"] for x in store
        ),
        "contract_flagger_false_flags_survive_every_gate": all(
            v == 1.0
            for st in table
            for a, v in table[st].get("contract_flagger", {}).get("clean", {}).items()
            if a in ARMS
        ),
    }


def _pct(x: float) -> str:
    return f"{100 * x:.0f}"


ARMS = ("agent", "contract", "crosscheck", "gate")


def render(summary: Mapping[str, Any]) -> str:
    table = summary["table"]
    claims = checks(table)
    lines = [
        "# The gate's numbers follow from the code (offline note, zero spend)",
        "",
        "Generated by `python -m tripwire.gate_determined` (about a minute, no network). No model",
        "was called: four scripted agents ran through the real harness, fault injector, gates and",
        f"adjudicator. {summary['runs']} runs, {summary['excluded']} excluded (a planned fault",
        "that did not fire).",
        "",
        "The agents: **oracle** always answers the true value, **wrong** always answers a",
        "confident wrong value, **value flagger** always refuses naming F1/F3/F7/F8, **contract",
        "flagger** always refuses naming a stale spec revision (F2). Each cell lists the four in",
        "that order, in percent. Faulted rows give the share scored correct; the clean row gives",
        "the share that raised a false flag. `agent` is the ungated run, `contract` and",
        "`crosscheck` are the two shadow gates scored on the same trajectory, and `gate` is v1's",
        "live gate condition run separately on the same scripted agent.",
        "",
        "Computed checks (each is recomputed from the table every time this note is generated):",
        "",
        *[f"- `{k}`: **{'yes' if v else 'NO'}**" for k, v in claims.items()],
        "",
    ]
    for study, by_policy in table.items():
        groups = sorted({g for p in by_policy.values() for g in p}, key=_group_key)
        arms = [a for a in ARMS if any(a in c for p in by_policy.values() for c in p.values())]
        lines += [f"## {study}", "", "| group | n | metric | " + " | ".join(arms) + " |"]
        lines.append("|" + "---|" * (3 + len(arms)))
        for g in groups:
            cell0 = next(by_policy[p][g] for p in POLICIES if g in by_policy.get(p, {}))
            vals = [
                " / ".join(
                    _pct(by_policy[p][g][a]) if g in by_policy.get(p, {}) else "-" for p in POLICIES
                )
                for a in arms
            ]
            lines.append(f"| {g} | {cell0['n']} | {cell0['metric']} | " + " | ".join(vals) + " |")
        lines.append("")
    return "\n".join(lines)


def _group_key(g: str) -> tuple[int, int, str]:
    if g == "clean":
        return (2, 0, g)
    if g.startswith("class:"):
        return (1, 0, g)
    return (0, int(g[1:]), g)


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--json", type=Path, default=None, help="also write the rates as JSON")
    args = p.parse_args(argv)
    summary = tabulate(run_all())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(summary))
    if args.json:
        args.json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(f"wrote {args.out} ({summary['runs']} runs, {summary['excluded']} excluded)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
