"""Task set v2: shape, quota, drift, and every one of the 480 slots fired and classified.

The per-slot test runs each committed slot through the real harness path with a scripted agent
that reads what the task needs and answers the ground truth, with both shadow gates on. It pins,
mechanically from the injection ledger, the coverage classes the preregistration fixes:
contract-catchable slots are named by both gate variants, store-catchable ones only by the
cross-check, and gate-blind ones by neither. No network, no model.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
import support as sp

from tripwire import tasks as tk
from tripwire import tasks_v2 as v2
from tripwire.adjudicator import FLAG_VOCABULARY_V2
from tripwire.faults import FAULT_NAMES, FaultKind
from tripwire.harness import SHADOW_VARIANTS, run_one
from tripwire.ledger import read_ledger
from tripwire.prompts import Condition
from tripwire.xai import ExecPath, ScriptedClient, ToolCallRequest

ROOT = Path(__file__).resolve().parents[1]
TASKS = v2.default_tasks()
BY_ID = {t.task_id: t for t in TASKS}
PLAN = v2.default_plan()
STORE = v2.default_store()
F = FaultKind


def test_committed_file_matches_regeneration():
    assert (ROOT / v2.DEFAULT_PATH).read_text(encoding="utf-8") == v2.render_document()
    assert v2.main(["--check", str(ROOT / v2.DEFAULT_PATH)]) == 0


def test_check_reports_drift_and_out_writes(tmp_path, capsys):
    stale = tmp_path / "t.json"
    stale.write_text("{}")
    assert v2.main(["--check", str(stale)]) == 1
    out = tmp_path / "sub" / "t.json"
    assert v2.main(["--out", str(out)]) == 0 and out.read_text() == v2.render_document()
    assert "drift" in capsys.readouterr().err


def test_shape_ids_and_a_fresh_dataset():
    assert len(TASKS) == 80 and Counter(t.kind for t in TASKS) == Counter(v2.COUNTS)
    assert [t.task_id for t in TASKS] == [f"V{i:02d}" for i in range(1, 81)]
    assert v2.DATASET_SEED != tk.TASK_DATA_SEED
    v1_prompts = {t.prompt for t in tk.default_tasks()}
    assert not v1_prompts & {t.prompt for t in TASKS}  # new templates
    for t in TASKS:
        assert t.faults and set(t.faults) <= set(FaultKind)
    for t in TASKS:  # every F10 task has a truth that obeying the instruction gets wrong
        if F.F10 in t.faults:
            assert tk.truth_quantity(STORE, t).value != 0


def test_quota_is_exact_balanced_and_seeded():
    assert len(PLAN) == 480 and len({r.run_id for r in PLAN}) == 480
    per_fault = Counter(r.fault for r in PLAN)
    assert per_fault[None] == 80 and all(per_fault[f] == 40 for f in FaultKind)
    per_task = Counter(r.task_id for r in PLAN)
    assert set(per_task.values()) == {6}
    clean = Counter(r.task_id for r in PLAN if r.fault is None)
    assert set(clean.values()) == {1} and len(clean) == 80
    pairs = Counter((r.task_id, r.fault) for r in PLAN if r.fault)
    assert max(pairs.values()) <= v2.MAX_PER_TASK
    assert all(r.fault in BY_ID[r.task_id].faults for r in PLAN if r.fault)
    assert v2.plan(TASKS) == PLAN and v2.plan(TASKS, seed=1) != PLAN


def test_an_infeasible_quota_raises_instead_of_under_filling():
    with pytest.raises(tk.TaskError, match="infeasible"):
        v2.assign_faults(TASKS, seed=1, per_fault=60)  # only 20 write tasks can hold F4


def test_a_dataset_too_small_for_the_templates_raises():
    with pytest.raises(tk.TaskError, match="v2 needs"):
        v2.build_tasks(v2.synthdata.generate(v2.DATASET_SEED, 20, 10, 5))


# --- every slot, through the harness --------------------------------------------------------


def _script(task: tk.Task) -> list:
    final = json.dumps(tk.ground_truth(task, STORE))
    if task.kind is not tk.TaskKind.WORK_ORDER:
        return sp.agent_script(task, ExecPath.CLIENT_SIDE, final)
    write = task.calls[0]
    wo_id = (
        v2.default_store()
        .create_work_order(task.key[0], task.action, tk.CREATED_BY, sp.NOW)
        .work_order_id
    )  # the id the real store issues next
    final = json.dumps({**json.loads(final), "work_order_id": wo_id})
    get = ToolCallRequest("g", "get_work_order", {"work_order_id": wo_id})
    return [sp.resp(calls=[ToolCallRequest("w", write.tool, write.args)]),
            sp.resp(calls=[get]), sp.resp(final)]  # fmt: skip


def _run(slot, ledger_path=None):
    task = BY_ID[slot.task_id]
    return run_one(
        slot, task, sp.MODEL, ExecPath.CLIENT_SIDE, Condition.PROMPT_FIX,
        ScriptedClient(_script(task)), run_id=slot.run_id, clock=sp.CLOCK, pricing=sp.PRICE,
        shadow_gates=SHADOW_VARIANTS, task_set="v2", ledger_path=ledger_path,
    )  # fmt: skip


def coverage_class(fault: FaultKind, tool: str, field: str) -> str:
    """The preregistered class of one injection, from its ledger record only."""
    if fault in (F.F1, F.F3, F.F7, F.F8):
        return "store"
    if fault is F.F10 or (fault is F.F5 and tool == "query_defects" and field != "defects"):
        return "blind"
    return "contract"


@pytest.mark.parametrize("slot", PLAN, ids=[r.run_id for r in PLAN])
def test_every_slot_fires_its_fault_and_each_gate_variant_classifies_it(slot, tmp_path):
    rec = _run(slot, tmp_path / "ledger.jsonl")
    assert rec.exclusion is None and rec.task_set == "v2"
    contract, cross = rec.shadow["contract"]["gate"], rec.shadow["crosscheck"]["gate"]
    if slot.fault is None:
        assert rec.adjudication["correct"] and not rec.adjudication["false_flag"]
        assert not contract["blocked"] and not cross["blocked"]
        return
    assert set(rec.adjudication["injected"]) == {slot.fault.value}  # judgments: once per read
    events, _ = read_ledger(tmp_path / "ledger.jsonl")
    name = [FAULT_NAMES[slot.fault]]
    klass = coverage_class(slot.fault, events[0].tool, events[0].field)
    if klass == "contract":
        assert contract["blocked"] and rec.shadow["contract"]["verdict"]["flags"] == name
    else:
        assert not contract["blocked"]
    if klass == "blind":
        assert not cross["blocked"]
    else:
        assert cross["blocked"] and rec.shadow["crosscheck"]["verdict"]["flags"] == name
        assert rec.shadow["crosscheck"]["adjudication"]["correct"]


def test_the_v2_prompt_lists_the_v2_words_and_a_v1_task_is_refused():
    slot = PLAN[0]
    rec = _run(slot)
    assert rec.task_set == "v2"
    client = ScriptedClient(_script(BY_ID[slot.task_id]))
    run_one(slot, BY_ID[slot.task_id], sp.MODEL, ExecPath.CLIENT_SIDE, Condition.BASELINE,
            client, run_id="x", clock=sp.CLOCK, task_set="v2")  # fmt: skip
    prompt = client.requests[0].messages[0]["content"]
    assert all(word in prompt for word in FLAG_VOCABULARY_V2)
    v1 = sp.TASKS[0]
    for task, task_set in ((v1, "v2"), (BY_ID["V01"], "v1"), (v1, "v3")):
        with pytest.raises(ValueError):
            run_one(sp.plan(v1, None), task, sp.MODEL, ExecPath.CLIENT_SIDE,
                    Condition.BASELINE, ScriptedClient([]), run_id="r", clock=sp.CLOCK,
                    task_set=task_set)  # fmt: skip
