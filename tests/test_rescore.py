"""Amendment A3 offline re-scoring matches what the harness now records."""

from __future__ import annotations

import json

import support as sp

from tripwire import rescore, xai
from tripwire import tasks as tk
from tripwire.faults import FaultKind
from tripwire.harness import run_one
from tripwire.prompts import Condition


def _rec(tmp_path, fault, condition=Condition.GATE):
    (tmp_path / "ledgers").mkdir(parents=True)
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    loop = [sp.resp(calls=sp.read_calls(task)) for _ in range(12)]
    rec = run_one(
        sp.plan(task, fault), task, sp.MODEL, xai.ExecPath.CLIENT_SIDE, condition,
        sp.client_for(loop), run_id="run-1", clock=sp.CLOCK, pricing=sp.PRICE,
        ledger_path=tmp_path / "ledgers" / "run-1.jsonl",
    )  # fmt: skip
    return json.loads(rec.to_json())


def _pre_a3(row):
    return {**row, "verdict": None, "adjudication": {**row["adjudication"], "correct": False}}


def test_rescore_turns_a_blocked_turn_cap_into_the_gate_report(tmp_path):
    now = _rec(tmp_path, FaultKind.F6)
    old = _pre_a3(now)
    assert rescore.a3_applies(old)
    new = json.loads(json.dumps(rescore.rescore([old], tmp_path / "ledgers")[0]))
    assert new["verdict"] == now["verdict"] and new["adjudication"] == now["adjudication"]
    assert new["gate"]["a3_turn_cap_report"] is True


def test_rescore_leaves_unblocked_ungated_and_answered_rows_alone(tmp_path):
    clean = _rec(tmp_path / "a", None)
    assert not rescore.a3_applies(clean)
    pf = _rec(tmp_path / "b", FaultKind.F6, Condition.PROMPT_FIX)
    assert not rescore.a3_applies(pf)
    answered = _rec(tmp_path / "c", FaultKind.F6)  # A3 already gave it a verdict
    rows = [clean, pf, answered]
    assert all(
        a is b for a, b in zip(rows, rescore.rescore(rows, tmp_path / "ledgers"), strict=True)
    )
