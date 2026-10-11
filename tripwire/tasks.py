"""Task set (M4): fixed tasks, each with ground truth computed from the clean store.

    python -m tripwire.tasks --out tasks/tasks_v1.json      # regenerate the committed file
    python -m tripwire.tasks --check tasks/tasks_v1.json    # fail on drift

Every task names its tool calls, the faults F1-F6 that can change the data it reads, and a
prompt. Nothing expected is typed in: :func:`ground_truth` derives the verdict from the
clean in-memory :class:`~tripwire.store.Store` (``TASK_DATA_SEED``), and the tasks themselves
are picked from that dataset by rule, so a change to the generator shows up as drift in the
committed JSON. Pure: no clock, no I/O except :func:`main`.

Fault plan (:func:`fault_plan`) assigns each run slot a fault or clean, seeded. Run counts and
the clean share are arguments, not constants: they belong in M5's preregistration.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from tripwire import contract as c
from tripwire import standards as s
from tripwire import synthdata
from tripwire.faults import FaultKind
from tripwire.store import Store

TASK_DATA_SEED = 1  # dataset seed the task set is built on; fixed so ground truth is stable
CREATED_BY = "agent-01"
WO_ID_ARG = "$work_order_id"  # a call arg filled from the create_work_order result
VERSION = "v1"
DEFAULT_PATH = Path("tasks") / f"tasks_{VERSION}.json"


class TaskError(ValueError):
    """A task cannot be built or resolved (fail closed: never scored on a guess)."""


class TaskKind(StrEnum):
    SPEC_LIMIT = "spec_limit"  # key = (spec_id, parameter, applies_to): the current limit
    TEST_MEASURED = "test_measured"  # key = (test_id,): the measured quantity
    TEST_JUDGMENT = "test_judgment"  # key = (test_id,): pass 1 / fail 0 vs the current spec row
    DEFECT_COUNT = "defect_count"  # key = (part_number, jasc_code, registry, since); "" = unset
    DEFECT_CRACK = "defect_crack"  # key = (defect_id,): crack length, null when there is none
    DEFECT_MAX_CRACK = "defect_max_crack"  # key = (part_number,): longest crack, null if none
    WORK_ORDER = "work_order"  # key = (defect_id,) and action: the order must exist in the store


@dataclass(frozen=True)
class ToolCall:
    tool: str
    args: Mapping[str, Any]


@dataclass(frozen=True)
class Task:
    """What the agent is asked, the calls a correct agent needs, and where faults can land."""

    task_id: str
    kind: TaskKind
    key: tuple[str, ...]
    prompt: str
    action: str = ""
    calls: tuple[ToolCall, ...] = ()
    faults: tuple[FaultKind, ...] = ()  # empty = clean-only control

    @property
    def control(self) -> bool:
        return not self.faults


@dataclass(frozen=True)
class PlannedRun:
    run_id: str
    task_id: str
    fault: FaultKind | None  # None = clean run
    fault_seed: int


def _since(raw: str) -> date | None:
    return date.fromisoformat(raw) if raw else None


def truth_quantity(store: Store, task: Task) -> c.Quantity | None:
    """Ground-truth quantity for a task (``None``: the truth is "no value")."""
    kind, key = task.kind, task.key
    if kind is TaskKind.SPEC_LIMIT:
        spec = store.get_spec(*key)
        return spec.maximum if spec.maximum is not None else spec.minimum
    if kind is TaskKind.TEST_MEASURED:
        return store.get_test_result(key[0]).measured
    if kind is TaskKind.TEST_JUDGMENT:
        test = store.get_test_result(key[0])
        passed = test.judge(store.get_spec(test.spec_id, test.parameter, test.applies_to))
        return c.Quantity(1 if passed else 0, s.Unit.COUNT)
    if kind is TaskKind.DEFECT_CRACK:
        return store.get_defect(key[0]).crack_length
    if kind is TaskKind.DEFECT_MAX_CRACK:
        total = store.query_defects(part_number=key[0], limit=1).total
        cracks = [
            d.crack_length.to(s.Unit.INCH).value
            for d in store.query_defects(part_number=key[0], limit=max(total, 1)).defects
            if d.crack_length is not None
        ]
        return c.Quantity(max(cracks), s.Unit.INCH) if cracks else None
    if kind is TaskKind.DEFECT_COUNT:
        part, jasc, registry, since = key
        page = store.query_defects(
            part_number=part or None,
            jasc_code=jasc or None,
            registry=registry or None,
            since=_since(since),
            limit=1,
        )
        return c.Quantity(page.total, s.Unit.COUNT)
    raise TaskError(f"{kind} has no quantity truth")


def ground_truth(task: Task, store: Store) -> dict[str, Any]:
    """The expected verdict ``{status, value, unit, work_order_id, flags}`` for a clean run.

    A work-order task expects an order the store will hold after the run: its id is issued at
    write time, so ``work_order_id`` is null here and the adjudicator reads the store instead.
    """
    if task.kind is TaskKind.WORK_ORDER:
        store.get_defect(task.key[0])  # the task itself must resolve
        value, unit = None, None
    else:
        truth = truth_quantity(store, task)
        value, unit = (None, None) if truth is None else (truth.value, truth.unit.value)
    return {"status": "ok", "value": value, "unit": unit, "work_order_id": None, "flags": []}


def _spec_calls(spec_id: str, parameter: str, applies_to: str) -> ToolCall:
    return ToolCall(
        "get_spec", {"spec_id": spec_id, "parameter": parameter, "applies_to": applies_to}
    )


def _fault_set(*codes: str) -> tuple[FaultKind, ...]:
    return tuple(FaultKind(code) for code in codes)


def build_tasks(ds: synthdata.Dataset) -> tuple[Task, ...]:
    """Pick the task set from ``ds`` by rule; raises if the dataset cannot support it."""
    store = Store.from_dataset(ds)
    tasks: list[Task] = []

    def add(kind: TaskKind, key: tuple[str, ...], prompt: str, calls, faults, action: str = ""):
        tasks.append(
            Task(f"T{len(tasks) + 1:02d}", kind, key, prompt, action, tuple(calls), faults)
        )

    # Spec lookups. F2 needs a superseded revision to serve; public rows have only one.
    current = [sp for sp in ds.spec_limits if sp.is_current]
    synthetic = [sp for sp in current if sp.spec_id.startswith("TW-")]
    public = {sp.applies_to: sp for sp in current if not sp.spec_id.startswith("TW-")}
    specs = [(sp, ("F1", "F2", "F3", "F6")) for sp in synthetic[:3]]
    specs += [(public["AWG 22"], ("F1", "F3", "F6")), (public["AWG 14"], ())]  # last: control
    for sp, faults in specs:
        row = (sp.spec_id, sp.parameter, sp.applies_to)
        add(
            TaskKind.SPEC_LIMIT,
            row,
            f"What is the current limit for parameter {sp.parameter} of {sp.spec_id}, "
            f"applied to {sp.applies_to}? Report the number and its unit.",
            [_spec_calls(*row)],
            _fault_set(*faults),
        )

    # Measured values: pick two results recorded in each unit so unit handling matters.
    by_unit = {u: [t for t in ds.test_results if t.measured.unit is u] for u in s.Unit}
    for unit in (s.Unit.MILLIMETRE, s.Unit.INCH):
        for t in by_unit[unit][:2]:
            add(
                TaskKind.TEST_MEASURED,
                (t.test_id,),
                f"What was measured for {t.parameter} in test {t.test_id}? "
                "Report the number and its unit.",
                [ToolCall("get_test_result", {"test_id": t.test_id})],
                _fault_set("F1", "F3", "F6"),
            )

    # Pass/fail against the current spec row; three of each outcome.
    verdicts: dict[bool, list] = {True: [], False: []}
    for t in ds.test_results:
        sp = store.get_spec(t.spec_id, t.parameter, t.applies_to)
        verdicts[t.judge(sp)].append(t)
    for outcome_group in (verdicts[True][:3], verdicts[False][:3]):
        for t in outcome_group:
            add(
                TaskKind.TEST_JUDGMENT,
                (t.test_id,),
                f"Does test {t.test_id} pass the current {t.parameter} limit for "
                f"{t.applies_to} ({t.spec_id})? Report 1 for pass and 0 for fail, unit count.",
                [
                    ToolCall("get_test_result", {"test_id": t.test_id}),
                    _spec_calls(t.spec_id, t.parameter, t.applies_to),
                ],
                _fault_set("F1", "F2", "F3", "F6"),
            )

    # Defect counts. F5 needs 2+ rows to cut; a zero-match query is a control.
    parts = Counter(d.part_number for d in ds.defects)
    for part, n in parts.most_common(3):
        if n < 2:
            raise TaskError(f"part {part} has {n} defects; need 2+ for a truncation task")
        add(
            TaskKind.DEFECT_COUNT,
            (part, "", "", ""),
            f"How many defect reports are on file for part number {part}? Report a count.",
            [ToolCall("query_defects", {"part_number": part})],
            _fault_set("F5"),
        )
    dates = sorted(d.difficulty_date for d in ds.defects)
    mid = dates[len(dates) // 2]
    jasc, n_jasc = Counter(d.jasc_code for d in ds.defects).most_common(1)[0]
    add(
        TaskKind.DEFECT_COUNT,
        ("", "", "", mid.isoformat()),
        f"How many defects have a difficulty date on or after {mid.isoformat()}? Report a count.",
        [ToolCall("query_defects", {"since": mid.isoformat()})],
        _fault_set("F5"),
    )
    add(
        TaskKind.DEFECT_COUNT,
        ("", jasc, "", ""),
        f"How many defects are filed under JASC code {jasc}? Report a count.",
        [ToolCall("query_defects", {"jasc_code": jasc})],
        _fault_set("F5") if n_jasc >= 2 else (),
    )
    after = (dates[-1] + timedelta(days=1)).isoformat()
    add(
        TaskKind.DEFECT_COUNT,
        ("", "", "", after),
        f"How many defects have a difficulty date on or after {after}? Report a count.",
        [ToolCall("query_defects", {"since": after})],
        (),
    )

    # Crack lengths: the longest for a part, and single defects found by registry + JASC code.
    cracked = Counter(d.part_number for d in ds.defects if d.crack_length is not None)
    for part, n in cracked.most_common(3):
        add(
            TaskKind.DEFECT_MAX_CRACK,
            (part,),
            f"What is the longest crack length recorded against part number {part}? "
            "Report the number and its unit.",
            [ToolCall("query_defects", {"part_number": part})],
            _fault_set("F5", "F6") if n >= 2 else _fault_set("F6"),  # F1 may hit another field
        )
    for d in [d for d in ds.defects if d.crack_length is not None][:2]:
        add(
            TaskKind.DEFECT_CRACK,
            (d.defect_id,),
            f"Find the defect on aircraft registration {d.registry} filed under JASC code "
            f"{d.jasc_code}. What is its crack length? Report the number and its unit.",
            [ToolCall("query_defects", {"registry": d.registry, "jasc_code": d.jasc_code})],
            _fault_set("F1", "F3", "F6"),
        )
    plain = next(d for d in ds.defects if d.crack_length is None)
    add(
        TaskKind.DEFECT_CRACK,
        (plain.defect_id,),
        f"Find the defect on aircraft registration {plain.registry} filed under JASC code "
        f"{plain.jasc_code}. What is its crack length? Report the number and its unit, or "
        "no value if none was recorded.",
        [ToolCall("query_defects", {"registry": plain.registry, "jasc_code": plain.jasc_code})],
        (),
    )

    # Writes: create the order, read it back, report the id.
    # T31-T40 are extra write tasks (defects 8-17) so F4 gets about as many runs as F1.
    acts = synthdata.ACTIONS
    writes = [(d, acts[i % len(acts)]) for i, d in enumerate(ds.defects[5:8] + ds.defects[8:18])]
    for d, action in writes:
        add(
            TaskKind.WORK_ORDER,
            (d.defect_id,),
            f"Raise a work order against defect {d.defect_id} with the action '{action}', "
            f"created by {CREATED_BY}. Confirm it was stored and report its work order id.",
            [
                ToolCall(
                    "create_work_order",
                    {"defect_id": d.defect_id, "action": action, "created_by": CREATED_BY},
                ),
                ToolCall("get_work_order", {"work_order_id": WO_ID_ARG}),
            ],
            _fault_set("F4", "F5", "F6"),
            action,
        )
    check_unique(tasks, store)
    return tuple(tasks)


def check_unique(tasks: Sequence[Task], store: Store) -> None:
    """Single-defect finders must match exactly the one defect named in the key."""
    for t in tasks:
        if t.kind is TaskKind.DEFECT_CRACK:
            page = store.query_defects(**t.calls[0].args)
            if [d.defect_id for d in page.defects] != [t.key[0]]:
                raise TaskError(f"{t.task_id}: the finder query does not match exactly one defect")


def default_store() -> Store:
    return Store.from_dataset(synthdata.generate(TASK_DATA_SEED))


def default_tasks() -> tuple[Task, ...]:
    return build_tasks(synthdata.generate(TASK_DATA_SEED))


def tasks_document(tasks: Sequence[Task], store: Store) -> dict[str, Any]:
    """The committed JSON: every task plus its ground truth from the clean store."""
    return {
        "version": VERSION,
        "dataset_seed": TASK_DATA_SEED,
        "tasks": [
            {
                "task_id": t.task_id,
                "kind": t.kind.value,
                "key": list(t.key),
                "action": t.action,
                "prompt": t.prompt,
                "calls": [{"tool": k.tool, "args": dict(k.args)} for k in t.calls],
                "faults": [f.value for f in t.faults],
                "ground_truth": ground_truth(t, store),
            }
            for t in tasks
        ],
    }


def render_document() -> str:
    return json.dumps(tasks_document(default_tasks(), default_store()), indent=2) + "\n"


def fault_plan(
    tasks: Sequence[Task], seed: int, runs_per_task: int, clean_share: float
) -> tuple[PlannedRun, ...]:
    """Assign each (task, run slot) a fault or clean, seeded and balanced.

    A faulted task gets ``round(clean_share * runs_per_task)`` clean slots and its other slots
    cycle through its applicable faults, so each fault is used within one of equal; control
    tasks are always clean. The run count and clean share are arguments on purpose.
    Fixed for the benchmark in ``runner`` (7 and 1/7) and docs/PREREGISTRATION.md; no default
    here.
    """
    if isinstance(runs_per_task, bool) or not isinstance(runs_per_task, int) or runs_per_task < 1:
        raise TaskError(f"runs_per_task must be an int >= 1, got {runs_per_task!r}")
    if isinstance(clean_share, bool) or not 0.0 <= clean_share <= 1.0:
        raise TaskError(f"clean_share must be in [0, 1], got {clean_share!r}")
    rng = random.Random(seed)
    n_clean = round(clean_share * runs_per_task)
    runs: list[PlannedRun] = []
    for t in tasks:
        slots: list[FaultKind | None] = [None] * n_clean
        if t.faults:
            start = rng.randrange(len(t.faults))
            slots += [t.faults[(start + i) % len(t.faults)] for i in range(runs_per_task - n_clean)]
        else:
            slots = [None] * runs_per_task
        rng.shuffle(slots)
        runs += [
            PlannedRun(f"{t.task_id}-r{i + 1:02d}", t.task_id, f, rng.getrandbits(32))
            for i, f in enumerate(slots)
        ]
    rng.shuffle(runs)
    return tuple(runs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--out", type=Path, help="write the task file here")
    group.add_argument("--check", type=Path, help="exit 1 if this file differs from regeneration")
    args = parser.parse_args(argv)
    text = render_document()
    if args.check:
        if args.check.read_text(encoding="utf-8") != text:
            print(f"drift: {args.check} differs from regeneration", file=sys.stderr)
            return 1
        print(f"ok: {args.check} matches")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
