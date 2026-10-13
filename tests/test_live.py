"""Live client (recorded HTTP only) and the live-eval driver. No network, no real key."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import support as sp

from tripwire import live_eval, runner, targets, xai

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
OK_BODY = {
    "model": "grok-4.7-served",
    "status": "completed",
    "output": [
        {"type": "reasoning", "encrypted_content": "opaque"},
        {"type": "function_call", "call_id": "c1", "name": "get_spec", "arguments": '{"a": 1}'},
        {"type": "message", "content": [{"type": "output_text", "text": "hi"}]},
    ],
    "usage": {"input_tokens": 100, "output_tokens": 20, "cost_in_usd_ticks": 123_000_000},
}
REQ = xai.ChatRequest(
    "grok-4.7",
    "medium",
    ({"role": "user", "content": "q"},),
    ({"type": "function", "function": {"name": "t", "description": "d", "parameters": {}}},),
)


class Fake:
    def __init__(self, status=200, body=None):
        self.status, self.body, self.sent = status, body, []

    def __call__(self, url, headers, data, timeout):
        self.sent.append((url, dict(headers), json.loads(data)))
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        return self.status, raw


def live(fake, confirmed=True):
    return xai.LiveClient("sk-test", fake, wire_confirmed=confirmed)


def test_request_and_parse_round_trip():
    fake = Fake(body=OK_BODY)
    resp = live(fake).chat(REQ)
    url, headers, body = fake.sent[0]
    assert url == xai.API_URL and headers["Authorization"] == "Bearer sk-test"
    assert body["reasoning"] == {"effort": "medium"}
    assert body["tools"] == [
        {"type": "function", "name": "t", "description": "d", "parameters": {}}
    ]
    assert (resp.served_model, resp.content, resp.input_tokens, resp.output_tokens) == (
        "grok-4.7-served", "hi", 100, 20,
    )  # fmt: skip
    assert resp.cost_in_usd_ticks == 123_000_000
    assert resp.tool_calls == (xai.ToolCallRequest("c1", "get_spec", {"a": 1}),)
    assert resp.output_items[0]["type"] == "reasoning"  # kept verbatim for the next turn


def test_follow_up_turn_echoes_output_items_and_tool_results():
    fake = Fake(body=OK_BODY)
    first = live(fake).chat(REQ)
    msgs = (
        *REQ.messages,
        {"role": "assistant", "content": "", "output_items": list(first.output_items)},
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
    )
    live(fake).chat(xai.ChatRequest("grok-4.7", None, msgs))
    sent = fake.sent[1][2]
    assert "reasoning" not in sent
    assert sent["input"][1:4] == list(first.output_items)
    assert sent["input"][-1] == {"type": "function_call_output", "call_id": "c1", "output": "{}"}


def test_refuses_until_wire_confirmed_and_without_calling():
    fake = Fake(body=OK_BODY)
    assert xai.WIRE_CONFIRMED is True  # confirmed from docs.x.ai on 2026-10-11 (see xai.py)
    with pytest.raises(xai.LiveCallsDisabled):
        live(fake, confirmed=False).chat(REQ)
    assert fake.sent == []
    with pytest.raises(xai.LiveCallsDisabled):
        xai.LiveClient("")


def test_server_side_path_and_bad_history_refused():
    remote = xai.RemoteMcp("https://x.invalid/mcp", "ENV")
    with pytest.raises(xai.LiveCallsDisabled):
        live(Fake()).chat(xai.ChatRequest("m", None, (), remote_mcp=remote))
    for bad in ({"role": "assistant", "content": "x"}, {"role": "system", "content": "x"}):
        with pytest.raises(xai.XaiError):
            live(Fake()).chat(xai.ChatRequest("m", None, (bad,)))


def test_key_is_never_shown():
    assert "sk-test" not in repr(live(Fake()))


@pytest.mark.parametrize(
    ("status", "exc"),
    [(429, xai.QuotaExhausted), (500, xai.XaiError), (401, xai.XaiError)],
)
def test_http_failures(status, exc):
    with pytest.raises(exc) as err:
        live(Fake(status, {"error": "secret detail sk-test"})).chat(REQ)
    assert "secret" not in str(err.value) and not isinstance(err.value, xai.UsageUnrecognised)


def _without(path):
    body = json.loads(json.dumps(OK_BODY))
    target = body
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    return body


@pytest.mark.parametrize(
    "path", [("usage",), ("usage", "cost_in_usd_ticks"), ("usage", "input_tokens"),
             ("usage", "output_tokens")],
)  # fmt: skip
def test_missing_usage_halts_the_runner_instead_of_guessing_cost(path):
    with pytest.raises(xai.UsageUnrecognised):
        live(Fake(body=_without(path))).chat(REQ)
    assert issubclass(xai.UsageUnrecognised, xai.QuotaExhausted)  # run_matrix halts on it


def test_malformed_replies_raise():
    def bad(**change):
        return {**json.loads(json.dumps(OK_BODY)), **change}

    for body in (
        b"not json", [1], bad(error={"m": 1}), bad(status="incomplete"), bad(model=""),
        bad(output="x"), bad(output=[{"type": "web_search_call"}]),
        bad(output=[{"type": "message", "content": [{"type": "refusal"}]}]),
        bad(output=[{"type": "function_call", "call_id": "c", "name": "n", "arguments": "{"}]),
        bad(output=[{"type": "function_call", "call_id": "c", "name": "n", "arguments": "[]"}]),
        bad(output=[{"type": "function_call", "name": "n", "arguments": "{}"}]),
        bad(usage={**OK_BODY["usage"], "cost_in_usd_ticks": True}),
        bad(usage={**OK_BODY["usage"], "cost_in_usd_ticks": -1}),
    ):  # fmt: skip
        with pytest.raises(xai.XaiError):
            live(Fake(body=body)).chat(REQ)


# --- driver ---------------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "PREREGISTRATION.md").write_text("protocol v1\n")
    subprocess.run([*GIT, "-C", str(root), "init", "-q"], check=True)
    subprocess.run([*GIT, "-C", str(root), "add", "."], check=True)
    subprocess.run([*GIT, "-C", str(root), "commit", "-qm", "p"], check=True)
    return root


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "spend_ledger.jsonl"
    runner.init_spend_ledger(path, sp.CLOCK)
    return path


def args(repo, ledger, tmp_path, **kw):
    base = {
        "condition-set": "baseline-prompt-fix", "budget-usd": "25",
        "confirm": live_eval.CONFIRM_PHRASE, "ledger-rows": "0",
    }  # fmt: skip
    base.update({k.replace("_", "-"): v for k, v in kw.items()})
    out = ["--repo", str(repo), "--spend-ledger", str(ledger), "--out", str(tmp_path / "out")]
    for k, v in base.items():
        out += [f"--{k}", str(v)]
    return out


def test_dry_run_exercises_the_full_path_and_leaves_the_real_ledger_alone(repo, ledger, tmp_path):
    before = ledger.read_text()
    code = live_eval.main([*args(repo, ledger, tmp_path), "--dry-run", "--max-jobs", "3"], env={})
    assert code == 0 and ledger.read_text() == before
    out = tmp_path / "out"
    result = json.loads((out / "result.json").read_text())
    assert result["ran_this_session"] == 3 and result["exit_name"] == "OK"
    assert len((out / "receipt.jsonl").read_text().splitlines()) == 3
    assert runner.read_spend(out / "dry_run_spend_ledger.jsonl") == pytest.approx(3 * 0.052)
    assert (out / "meta.json").exists()


def test_gate_condition_set_plans_only_gate_jobs(repo, ledger, tmp_path):
    live_eval.main(
        [*args(repo, ledger, tmp_path, condition_set="gate"), "--dry-run", "--max-jobs", "1"]
    )
    rec = json.loads((tmp_path / "out/receipt.jsonl").read_text().splitlines()[0])
    assert rec["condition"] == "gate" and rec["gate"] is not None


@pytest.mark.parametrize(
    "change",
    [{"confirm": "yes"}, {"budget_usd": "0"}, {"budget_usd": "50.01"}, {"ledger_rows": "1"}],
)
def test_driver_refuses_bad_inputs_before_running(repo, ledger, tmp_path, change, capsys):
    assert live_eval.main([*args(repo, ledger, tmp_path, **change), "--dry-run"], env={}) == 2
    assert "REFUSED" in capsys.readouterr().err and not (tmp_path / "out").exists()


def test_budget_must_fit_the_room_left_under_the_ceiling(repo, ledger, tmp_path):
    with ledger.open("a") as fh:
        fh.write(json.dumps({"kind": "spend", "cost_usd": 30.0}) + "\n")
    base = args(repo, ledger, tmp_path, ledger_rows="1")
    assert live_eval.main([*base, "--dry-run", "--budget-usd", "20.01"]) == 2
    assert live_eval.check_budget(20.0, ledger, 1) == pytest.approx(20.0)


def test_missing_or_corrupt_ledger_refuses(repo, ledger, tmp_path):
    ledger.write_text("garbage\n")
    assert live_eval.main([*args(repo, ledger, tmp_path), "--dry-run"]) == 2
    ledger.unlink()
    assert live_eval.main([*args(repo, ledger, tmp_path), "--dry-run"]) == 2


def test_live_mode_needs_key_and_a_confirmed_wire_and_runs_nothing(
    repo, ledger, tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(xai, "WIRE_CONFIRMED", False)
    argv = [*args(repo, ledger, tmp_path), "--no-prior-paid-run"]
    assert live_eval.main(argv, env={}) == 2
    assert "XAI_API_KEY is not set" in capsys.readouterr().err
    assert live_eval.main(argv, env={"XAI_API_KEY": "sk-test"}) == 2
    err = capsys.readouterr().err
    assert "WIRE_CONFIRMED" in err and "sk-test" not in err
    assert not (tmp_path / "out").exists() and runner.read_spend(ledger) == 0.0


def test_confirmed_wire_reaches_the_client_and_spends_into_the_real_ledger(
    repo, ledger, tmp_path, monkeypatch
):
    monkeypatch.setattr(xai, "WIRE_CONFIRMED", True)
    monkeypatch.setattr(xai, "LiveClient", lambda key: live_eval.DryRunClient())
    real_plan = runner.plan_matrix
    monkeypatch.setattr(runner, "plan_matrix", lambda *a, **k: real_plan(*a, **k)[:3])
    argv = [*args(repo, ledger, tmp_path), "--budget-usd", "1", "--no-prior-paid-run"]
    assert live_eval.main(argv, env={"XAI_API_KEY": "k"}) == int(runner.ExitCode.OK)
    assert runner.read_spend(ledger) == pytest.approx(3 * 0.052)


def test_max_jobs_is_dry_run_only_and_uncommitted_protocol_refused(repo, ledger, tmp_path):
    assert live_eval.main([*args(repo, ledger, tmp_path), "--max-jobs", "1"], env={}) == 2
    (repo / "docs" / "PREREGISTRATION.md").write_text("edited\n")
    assert live_eval.main([*args(repo, ledger, tmp_path), "--dry-run"], env={}) == 2


def test_committed_spend_ledger_is_valid_and_the_workflow_is_manual_only():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    assert runner.read_spend(root / "runs" / "spend_ledger.jsonl") >= 0.0
    text = (root / "docs" / "live-eval.workflow.yml").read_text()
    assert "workflow_dispatch:" in text and "cancel-in-progress: false" in text
    for trigger in ("push:", "pull_request:", "schedule:"):
        assert trigger not in text.split("permissions:")[0].split("\non:")[1]
    # '' is falsy, so `dry && '' || secret` always yields the secret: the order below matters
    assert "XAI_API_KEY: ${{ !inputs.dry_run && secrets.XAI_API_KEY || '' }}" in text
    assert "inputs.dry_run && '' ||" not in text
    assert "actions: read" in text and "retention-days: 90" in text
    assert "live-eval-$kind-$RUN_ID" in text and "kind=paid" in text and "kind=dry" in text
    assert "pilot_jobs:" in text and "--no-prior-paid-run" in text and "--prior-ledger" in text
    assert "cp docs/live-eval.workflow.yml .github/workflows/live-eval.yml" in text
    # issue #46: a manual-only targeted-run input, wired to the committed job list
    assert "targets:" in text and 'options: ["", gate-confirm-1]' in text
    assert "--targets runs/targets/$TARGETS.json" in text
    # the installed workflow must stay byte-identical to this committed source
    installed = root / ".github" / "workflows" / "live-eval.yml"
    assert installed.read_text() == text


def _spend(run_id, cost, ts="2026-10-12T00:00:00+00:00"):
    return {"kind": "spend", "run_id": run_id, "cost_usd": cost, "timestamp": ts}


def _write_rows(path, *rows):
    with path.open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_prior_ledger_must_be_contained_in_the_committed_one(repo, ledger, tmp_path, capsys):
    prior = tmp_path / "prior.jsonl"
    prior.write_text(ledger.read_text())
    _write_rows(prior, _spend("a", 1.0), _spend("b", 2.0))
    base = [*args(repo, ledger, tmp_path), "--dry-run", "--max-jobs", "1"]
    assert live_eval.main([*base, "--prior-ledger", str(prior)], env={}) == 2  # forgotten commit
    assert "lacks 2 spend row" in capsys.readouterr().err
    _write_rows(ledger, _spend("a", 1.0))  # partially committed
    assert live_eval.main([*base, "--prior-ledger", str(prior), "--ledger-rows", "1"]) == 2
    _write_rows(ledger, _spend("b", 2.0))
    ok = [*args(repo, ledger, tmp_path, ledger_rows=2), "--dry-run", "--max-jobs", "1"]
    assert live_eval.main([*ok, "--prior-ledger", str(prior)], env={}) == 0


def test_prior_rows_match_on_run_id_timestamp_and_cost(repo, ledger, tmp_path):
    prior = tmp_path / "prior.jsonl"
    prior.write_text(ledger.read_text())
    _write_rows(prior, _spend("a", 1.0))
    for changed in (
        _spend("x", 1.0),
        _spend("a", 1.0, "2026-10-13T00:00:00+00:00"),
        _spend("a", 1.5),
    ):
        committed = tmp_path / "c.jsonl"
        committed.write_text(ledger.read_text())
        _write_rows(committed, changed, _spend("pad", 9.0))  # bigger total, wrong identity
        with pytest.raises(runner.RunnerRefused, match="lacks 1"):
            live_eval.check_prior_ledger(committed, prior)
        committed.unlink()
    _write_rows(ledger, _spend("a", 1.0))
    live_eval.check_prior_ledger(ledger, prior)  # same row: fine


def test_committed_total_below_prior_total_is_refused(tmp_path):
    prior, committed = tmp_path / "p.jsonl", tmp_path / "c.jsonl"
    for path in (prior, committed):
        runner.init_spend_ledger(path, sp.CLOCK)
    _write_rows(prior, _spend("a", 1.0), _spend("a", 1.0))  # the same run booked twice
    _write_rows(committed, _spend("a", 1.0))  # every identity present, but the total is lower
    with pytest.raises(runner.RunnerRefused, match="total is below"):
        live_eval.check_prior_ledger(committed, prior)


def test_live_runs_need_a_prior_decision_and_the_flags_are_exclusive(repo, ledger, tmp_path):
    argv = args(repo, ledger, tmp_path)
    assert live_eval.main(argv, env={"XAI_API_KEY": "k"}) == 2  # neither flag
    with pytest.raises(SystemExit):
        live_eval.main([*argv, "--no-prior-paid-run", "--prior-ledger", str(ledger)])
    assert not (tmp_path / "out").exists()


def test_pilot_runs_a_stratified_subset_and_is_marked(repo, ledger, tmp_path):
    base = [*args(repo, ledger, tmp_path), "--dry-run", "--pilot-jobs", "12"]
    assert live_eval.main(base, env={}) == 0
    out = tmp_path / "out"
    meta = json.loads((out / "meta.json").read_text())
    result = json.loads((out / "result.json").read_text())
    assert meta["pilot"] is True and meta["pilot_jobs"] == 12 and meta["jobs"] == 12
    assert result["pilot"] is True and result["ran_this_session"] == 12
    recs = [json.loads(x) for x in (out / "receipt.jsonl").read_text().splitlines()]
    assert {r["condition"] for r in recs} == {"baseline", "prompt_fix"}
    assert len({(r["condition"], r["planned_fault"]) for r in recs}) == 12
    # a pilot directory is never reused for a scored run
    assert live_eval.main([*args(repo, ledger, tmp_path), "--dry-run"], env={}) == 2


@pytest.mark.parametrize(("pilot", "budget"), [("9", "2"), ("21", "2"), ("10", "2.01"), ("0", "2")])
def test_live_pilot_bounds(repo, ledger, tmp_path, monkeypatch, pilot, budget):
    monkeypatch.setattr(xai, "LiveClient", lambda key: live_eval.DryRunClient())
    argv = [*args(repo, ledger, tmp_path, budget_usd=budget), "--no-prior-paid-run"]
    argv += ["--pilot-jobs", pilot]
    assert live_eval.main(argv, env={"XAI_API_KEY": "k"}) == 2
    assert not (tmp_path / "out").exists() and runner.read_spend(ledger) == 0.0


def test_live_pilot_inside_bounds_runs(repo, ledger, tmp_path, monkeypatch):
    monkeypatch.setattr(xai, "LiveClient", lambda key: live_eval.DryRunClient())
    argv = [*args(repo, ledger, tmp_path, budget_usd="2"), "--no-prior-paid-run"]
    both = [*argv, "--pilot-jobs", "10", "--max-jobs", "3"]
    assert live_eval.main(both, env={"XAI_API_KEY": "k"}) == 2  # exclusive with --max-jobs
    assert live_eval.main([*argv, "--pilot-jobs", "10"], env={"XAI_API_KEY": "k"}) == 0
    assert runner.read_spend(ledger) == pytest.approx(10 * 0.052)
    assert json.loads((tmp_path / "out/meta.json").read_text())["pilot_jobs"] == 10


# --- targeted confirmation runs (issue #46) -------------------------------------------------


def _targets_file(tmp_path, skipped, topup=(), name="gate-confirm-1"):
    plan = targets.gate_plan()
    chosen = set(skipped) | set(topup)
    doc = {
        "name": name, "condition_set": "gate", "skipped_job_ids": list(skipped),
        "topup_job_ids": list(topup),
        "job_ids": [j.job_id for j in plan if j.job_id in chosen],
    }  # fmt: skip
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(doc))
    return path


def test_targets_run_executes_only_the_listed_jobs(repo, ledger, tmp_path):
    plan = targets.gate_plan()
    tf = _targets_file(tmp_path, [j.job_id for j in plan[:3]])
    base = args(repo, ledger, tmp_path, condition_set="gate", budget_usd="4")
    assert live_eval.main([*base, "--dry-run", "--targets", str(tf)], env={}) == 0
    out = tmp_path / "out"
    result = json.loads((out / "result.json").read_text())
    meta = json.loads((out / "meta.json").read_text())
    assert result["ran_this_session"] == 3 and result["targets"] == "gate-confirm-1"
    assert meta["targets"] == "gate-confirm-1" and meta["target_count"] == 3
    recs = [json.loads(x) for x in (out / "receipt.jsonl").read_text().splitlines()]
    assert {r["run_id"] for r in recs} == {j.job_id for j in plan[:3]}


def test_committed_gate_confirm_1_runs_sixty_jobs_within_cap(repo, ledger, tmp_path):
    committed = Path(__file__).resolve().parent.parent / "runs/targets/gate-confirm-1.json"
    base = args(repo, ledger, tmp_path, condition_set="gate", budget_usd="4")
    assert live_eval.main([*base, "--dry-run", "--targets", str(committed)], env={}) == 0
    result = json.loads((tmp_path / "out/result.json").read_text())
    assert result["ran_this_session"] == 60
    assert runner.read_spend(tmp_path / "out/dry_run_spend_ledger.jsonl") == pytest.approx(
        60 * 0.052
    )


def test_targets_budget_capped_at_four(repo, ledger, tmp_path, capsys):
    tf = _targets_file(tmp_path, [targets.gate_plan()[0].job_id])
    base = args(repo, ledger, tmp_path, condition_set="gate", budget_usd="4.01")
    assert live_eval.main([*base, "--dry-run", "--targets", str(tf)], env={}) == 2
    assert "budget<=4.0" in capsys.readouterr().err


def test_targets_refuses_estimate_over_budget(repo, ledger, tmp_path, capsys):
    committed = Path(__file__).resolve().parent.parent / "runs/targets/gate-confirm-1.json"
    base = args(repo, ledger, tmp_path, condition_set="gate", budget_usd="1")
    assert live_eval.main([*base, "--dry-run", "--targets", str(committed)], env={}) == 2
    err = capsys.readouterr().err
    assert "estimate" in err and "exceeds budget" in err


def test_targets_condition_set_must_match(repo, ledger, tmp_path, capsys):
    tf = _targets_file(tmp_path, [targets.gate_plan()[0].job_id])
    base = args(repo, ledger, tmp_path, condition_set="baseline-prompt-fix", budget_usd="4")
    assert live_eval.main([*base, "--dry-run", "--targets", str(tf)], env={}) == 2
    assert "condition-set" in capsys.readouterr().err


def test_targets_refuses_a_stale_job_id(repo, ledger, tmp_path, capsys):
    tf = tmp_path / "stale.json"
    tf.write_text(
        json.dumps(
            {
                "name": "x",
                "condition_set": "gate",
                "skipped_job_ids": ["ghost-job"],
                "topup_job_ids": [],
                "job_ids": ["ghost-job"],
            }
        )  # fmt: skip
    )
    base = args(repo, ledger, tmp_path, condition_set="gate", budget_usd="4")
    assert live_eval.main([*base, "--dry-run", "--targets", str(tf)], env={}) == 2
    assert "not in the gate plan" in capsys.readouterr().err


def test_targets_excludes_pilot_and_max_jobs(repo, ledger, tmp_path, capsys):
    tf = _targets_file(tmp_path, [targets.gate_plan()[0].job_id])
    base = args(repo, ledger, tmp_path, condition_set="gate", budget_usd="4")
    assert (
        live_eval.main([*base, "--dry-run", "--targets", str(tf), "--max-jobs", "1"], env={}) == 2
    )
    assert "--targets excludes" in capsys.readouterr().err
