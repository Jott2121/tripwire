"""Task set: truth resolves and adjudicates, faults bite, the plan is seeded, JSON is current."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from tripwire import server as srv
from tripwire import tasks as tk
from tripwire.adjudicator import FLAG_VOCABULARY, Verdict, adjudicate
from tripwire.faults import FaultConfig, FaultInjector, FaultKind, FaultSpec
from tripwire.ledger import Ledger
from tripwire.prompts import answer_format, render_prompt
from tripwire.tasks import Task, TaskError, TaskKind, ToolCall

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
TASKS = tk.default_tasks()
COMMITTED = Path(__file__).resolve().parent.parent / "tasks" / "tasks_v1.json"


def _store():
    return tk.default_store()


def _serve(task, faults=()):
    """Run the task's calls on a fresh store; returns (served texts, ledger, run id)."""
    store, ledger = _store(), Ledger()
    cfg = FaultConfig(7, tuple(FaultSpec(FaultKind(f)) for f in faults))
    inj = FaultInjector(cfg, ledger, "run", store, lambda: NOW) if faults else None
    mcp = srv.build_mcp(store, lambda: NOW, inj)

    async def run():
        out, wo_id = [], None
        async with create_connected_server_and_client_session(mcp) as session:
            for call in task.calls:
                args = {k: (wo_id if v == tk.WO_ID_ARG else v) for k, v in call.args.items()}
                res = await session.call_tool(call.tool, args)
                text = res.content[0].text
                out.append((res.isError, text))
                if call.tool == "create_work_order" and not res.isError:
                    wo_id = json.loads(text)["work_order"]["work_order_id"]
        return out

    return asyncio.run(run()), ledger, store


def test_task_set_shape():
    assert 28 <= len(TASKS) <= 40  # T31-T40 are the extra write tasks that give F4 its runs
    assert len({t.task_id for t in TASKS}) == len(TASKS)
    assert {t.kind for t in TASKS} == set(TaskKind)
    assert {k.tool for t in TASKS for k in t.calls} == set(srv.TOOLS)
    assert all(t.calls and t.prompt for t in TASKS)
    assert sum(t.control for t in TASKS) >= 3
    per_fault = Counter(f for t in TASKS for f in t.faults)
    assert all(per_fault[f] >= 3 for f in FaultKind), per_fault


def test_ground_truth_resolves_and_adjudicates_correct_on_a_clean_run():
    for t in TASKS:
        store = _store()
        truth = tk.ground_truth(t, store)
        if t.kind is TaskKind.WORK_ORDER:  # the id is issued at write time
            wo = store.create_work_order(t.key[0], t.action, tk.CREATED_BY, NOW)
            truth["work_order_id"] = wo.work_order_id
        led = Ledger()
        a = adjudicate(led.events, led.head, store, t, Verdict.from_dict(truth), "clean")
        assert (a.correct, a.acted, a.false_flag, a.injected) == (True, False, False, ()), t


def test_ground_truth_is_derived_not_typed():
    store = _store()
    by_id = {t.task_id: t for t in TASKS}
    zero = next(t for t in TASKS if t.kind is TaskKind.DEFECT_COUNT and t.control)
    assert tk.ground_truth(zero, store)["value"] == 0
    kinds = {t.kind for t in TASKS if tk.ground_truth(t, store)["value"] is None}
    assert kinds == {TaskKind.WORK_ORDER, TaskKind.DEFECT_CRACK}
    judged = {tk.ground_truth(t, store)["value"] for t in TASKS if t.kind is TaskKind.TEST_JUDGMENT}
    assert judged == {0, 1}
    assert len(by_id) == len(TASKS)


def test_a_wrong_answer_or_phantom_order_is_not_correct():
    store = _store()
    led = Ledger()
    for t in TASKS:
        truth = tk.ground_truth(t, store)
        if t.kind is TaskKind.WORK_ORDER:
            truth["work_order_id"] = "WO-900001"
        elif truth["value"] is None:
            truth["value"], truth["unit"] = 1.0, "in"
        else:
            truth["value"] = truth["value"] + 1
        a = adjudicate(led.events, led.head, store, t, Verdict.from_dict(truth), "clean")
        assert not a.correct, t


RELEVANT = {
    TaskKind.TEST_MEASURED: "measured",
    TaskKind.DEFECT_CRACK: "crack_length",
    TaskKind.DEFECT_MAX_CRACK: "crack_length",
}
_APPLICABLE = [(t, f) for t in TASKS for f in t.faults]


@pytest.mark.parametrize(("task", "fault"), _APPLICABLE, ids=lambda x: getattr(x, "task_id", x))
def test_each_applicable_fault_changes_the_served_data(task, fault):
    clean, _, _ = _serve(task)
    served, ledger, _ = _serve(task, [fault.value])
    assert served != clean
    assert ledger.events
    assert {e.fault for e in ledger.events} == {fault.value}
    if fault in (FaultKind.F1, FaultKind.F3):  # the planted change lands on the answer's field
        want = RELEVANT.get(task.kind)
        assert want is None or all(want in e.field for e in ledger.events), ledger.events


def test_clean_only_controls_have_nothing_to_corrupt_or_are_excluded_by_design():
    assert all(not t.faults for t in TASKS if t.control)


def test_prompts_use_the_adjudicator_vocabulary_and_do_not_claim_injection():
    text = answer_format()
    assert all(word in text for word in FLAG_VOCABULARY)
    for t in TASKS:
        body = render_prompt(t)
        assert body.startswith(t.prompt)
        assert not any(w in t.prompt.lower() for w in ("fault", "inject", "corrupt", "tamper"))
    assert "inject" not in text.lower()


def test_plan_is_seeded_balanced_and_never_faults_a_control():
    a = tk.fault_plan(TASKS, 5, runs_per_task=4, clean_share=0.5)
    assert a == tk.fault_plan(TASKS, 5, runs_per_task=4, clean_share=0.5)
    assert a != tk.fault_plan(TASKS, 6, runs_per_task=4, clean_share=0.5)
    assert len(a) == 4 * len(TASKS) and len({r.run_id for r in a}) == len(a)
    by_task = {t.task_id: t for t in TASKS}
    for t in TASKS:
        mine = [r for r in a if r.task_id == t.task_id]
        clean = sum(r.fault is None for r in mine)
        assert clean == (4 if t.control else 2)
        assert all(r.fault in t.faults for r in mine if r.fault)
    assert all(r.task_id in by_task for r in a)
    faults = Counter(r.fault for r in a if r.fault)
    assert set(faults) == set(FaultKind)  # every fault is exercised


def test_plan_extremes_and_bad_arguments():
    none = tk.fault_plan(TASKS, 1, runs_per_task=3, clean_share=1.0)
    assert all(r.fault is None for r in none)
    full = tk.fault_plan(TASKS, 1, runs_per_task=3, clean_share=0.0)
    assert sum(r.fault is None for r in full) == 3 * sum(t.control for t in TASKS)
    for bad in ({"runs_per_task": 0}, {"runs_per_task": True}, {"clean_share": 1.5}):
        kw = {"runs_per_task": 2, "clean_share": 0.5} | bad
        with pytest.raises(TaskError):
            tk.fault_plan(TASKS, 1, **kw)


def test_builder_fails_closed_on_an_ambiguous_finder():
    ambiguous = Task(
        "X", TaskKind.DEFECT_CRACK, ("nope",), "p", calls=(ToolCall("query_defects", {}),)
    )
    with pytest.raises(TaskError, match="exactly one"):
        tk.check_unique([ambiguous], _store())
    with pytest.raises(TaskError, match="no quantity"):
        tk.truth_quantity(_store(), Task("X", TaskKind.WORK_ORDER, ("d",), "p"))


def test_committed_json_matches_regeneration(tmp_path, capsys):
    assert COMMITTED.read_text(encoding="utf-8") == tk.render_document()
    assert tk.main(["--check", str(COMMITTED)]) == 0
    drifted = tmp_path / "t.json"
    drifted.write_text("{}", encoding="utf-8")
    assert tk.main(["--check", str(drifted)]) == 1
    out = tmp_path / "sub" / "t.json"
    assert tk.main(["--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == tk.render_document()
    capsys.readouterr()
