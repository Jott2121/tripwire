"""One run on both execution paths, with a scripted xAI client."""

from __future__ import annotations

import json

import pytest
import support as sp

from tripwire import tasks as tk
from tripwire import xai
from tripwire.faults import FaultKind
from tripwire.harness import (
    MAX_TURNS,
    Exclusion,
    GateUnavailable,
    InProcessTools,
    extract_verdict,
    run_one,
)
from tripwire.prompts import PROMPT_FIX_TEXT, Condition, condition_prompt
from tripwire.xai import ExecPath


def _run(task, fault, path, client, condition=Condition.BASELINE, **kw):
    return run_one(
        sp.plan(task, fault), task, sp.MODEL, path, condition, client,
        run_id="run-1", clock=sp.CLOCK, pricing=sp.PRICE, **kw,
    )  # fmt: skip


@pytest.mark.parametrize("path", list(ExecPath))
def test_clean_run_is_correct_on_both_paths_with_every_required_field(path):
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    client = sp.client_for(sp.agent_script(task, path, sp.truth_json(task)))
    rec = _run(task, None, path, client)
    assert rec.adjudication["correct"] and not rec.adjudication["false_flag"]
    assert rec.exclusion is None and rec.error is None
    assert rec.served_models == ("served-id",) * rec.turns
    assert (rec.model_requested, rec.effort, rec.path) == (
        "test-model-id",
        "test-effort",
        path.value,
    )
    assert rec.input_tokens == 10 * rec.turns and rec.output_tokens == 5 * rec.turns
    assert rec.cost_usd == pytest.approx(10 * rec.turns * 50 / 1e6)
    assert rec.timestamp == sp.NOW.isoformat() and rec.latency_s >= 0
    assert rec.tool_calls == tuple(c.name for c in sp.read_calls(task))
    assert json.loads(rec.to_json())["run_id"] == "run-1"


def test_the_two_paths_produce_the_same_score():
    task = sp.first_task(tk.TaskKind.TEST_MEASURED)
    scores = []
    for path in ExecPath:
        client = sp.client_for(sp.agent_script(task, path, sp.truth_json(task)))
        scores.append(_run(task, None, path, client).adjudication)
    assert scores[0] == scores[1]


@pytest.mark.parametrize("path", list(ExecPath))
def test_faulted_run_naming_the_fault_is_a_correct_safe_refusal(path):
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT, FaultKind.F1)
    client = sp.client_for(sp.agent_script(task, path, sp.refusal_json("F1")))
    rec = _run(task, FaultKind.F1, path, client)
    assert rec.adjudication["injected"] == ("F1",) and rec.exclusion is None
    assert rec.adjudication["flagged"] and rec.adjudication["correct"]


def test_work_order_task_writes_through_the_server():
    task = sp.first_task(tk.TaskKind.WORK_ORDER)
    client = sp.client_for(sp.agent_script(task, ExecPath.CLIENT_SIDE, sp.truth_json(task)))
    rec = _run(task, None, ExecPath.CLIENT_SIDE, client)
    assert rec.adjudication["correct"]


def test_planned_fault_that_never_fires_is_injection_unverified_not_dropped():
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT, FaultKind.F1)
    client = sp.client_for([sp.resp(sp.truth_json(task))])  # answers without reading any data
    rec = _run(task, FaultKind.F1, ExecPath.CLIENT_SIDE, client)
    assert rec.exclusion == Exclusion.INJECTION_UNVERIFIED.value
    assert rec.adjudication is not None


def test_unparseable_reply_is_scored_incorrect_and_kept():
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    rec = _run(task, None, ExecPath.CLIENT_SIDE, sp.client_for([sp.resp("I cannot say")]))
    assert rec.verdict is None and rec.exclusion is None
    assert rec.adjudication["correct"] is False and rec.adjudication["detection"] is None


def test_turn_cap_ends_the_run_without_a_verdict():
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    loop = [sp.resp(calls=sp.read_calls(task)) for _ in range(MAX_TURNS)]
    rec = _run(task, None, ExecPath.CLIENT_SIDE, sp.client_for(loop))
    assert rec.turns == MAX_TURNS and rec.verdict is None
    assert rec.adjudication["correct"] is False


def test_client_error_is_run_error_and_quota_propagates():
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    rec = _run(task, None, ExecPath.CLIENT_SIDE, xai.ScriptedClient([xai.XaiError("boom")]))
    assert rec.exclusion == Exclusion.RUN_ERROR.value and "boom" in rec.error
    assert rec.adjudication is None
    with pytest.raises(xai.QuotaExhausted):
        _run(task, None, ExecPath.CLIENT_SIDE, xai.ScriptedClient([xai.QuotaExhausted("out")]))


def test_gate_condition_is_refused_until_the_gate_exists():
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    with pytest.raises(GateUnavailable):
        _run(task, None, ExecPath.CLIENT_SIDE, xai.ScriptedClient([]), Condition.GATE)


def test_ledger_is_written_when_a_path_is_given(tmp_path):
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT, FaultKind.F1)
    client = sp.client_for(sp.agent_script(task, ExecPath.CLIENT_SIDE, sp.refusal_json("F1")))
    _run(task, FaultKind.F1, ExecPath.CLIENT_SIDE, client, ledger_path=tmp_path / "l.jsonl")
    assert (tmp_path / "l.jsonl").read_text().strip()


def test_tools_expose_five_specs_and_return_errors_as_data():
    task = sp.first_task(tk.TaskKind.SPEC_LIMIT)
    from tripwire.faults import FaultConfig, FaultInjector
    from tripwire.ledger import Ledger

    store = tk.default_store()
    inj = FaultInjector(FaultConfig(1), Ledger(), "r", store, sp.CLOCK)
    tools = InProcessTools(store, inj, sp.CLOCK)
    assert len(tools.specs()) == 5
    assert "error" in tools.call("get_work_order", {"work_order_id": "nope"})
    assert "error" not in tools.call(sp.read_calls(task)[0].name, dict(sp.read_calls(task)[0].args))


def test_extract_verdict_takes_the_last_valid_object():
    good = '{"status": "ok", "value": 1, "unit": "in", "flags": []}'
    assert extract_verdict(f'{{"x": 1}} then {good} trailing').value == 1
    assert extract_verdict('{"status": "bogus"}') is None
    assert extract_verdict("{not json") is None


def test_only_the_warned_conditions_say_data_may_be_wrong():
    task = sp.TASKS[0]
    base = condition_prompt(task, Condition.BASELINE)
    assert PROMPT_FIX_TEXT not in base
    assert "may be wrong" not in base and "inconsisten" not in base
    assert "inject" not in base.lower()
    for cond in (Condition.PROMPT_FIX, Condition.GATE):
        assert PROMPT_FIX_TEXT in condition_prompt(task, cond)
    assert condition_prompt(task, Condition.GATE) == condition_prompt(task, Condition.PROMPT_FIX)


def test_xai_placeholders_are_unresolved_and_live_calls_refused():
    assert all(not m.resolved for m in xai.MODEL_CONFIGS)
    assert all(p is None for p in xai.PRICING.values())
    req = xai.ChatRequest("m", None, ())
    with pytest.raises(xai.LiveCallsDisabled):
        xai.LiveClient().chat(req)
    with pytest.raises(xai.XaiError):
        xai.ScriptedClient([]).chat(req)
    remote = xai.RemoteMcp("https://example.invalid/mcp", "TRIPWIRE_SERVER_TOKEN")
    call = xai.ToolCallRequest("c", "get_spec", {})
    with pytest.raises(xai.XaiError):
        xai.ScriptedClient([sp.resp(remote=[call])]).chat(
            xai.ChatRequest("m", None, (), (), remote)
        )
