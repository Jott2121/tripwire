"""The phase two live runner on synthetic real-shaped files: every check, no network, no key."""

from __future__ import annotations

import csv
import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

from tests.test_registry import ACFTREF, MASTER_HEADER, master_row
from tripwire import clients, live_phase2, mining, phase2
from tripwire.synth import make_sdr_rows, write_sdr_csv
from tripwire.xai import ChatResponse, QuotaExhausted, XaiError

_FREEZE = Path(__file__).resolve().parents[1] / "scripts" / "freeze_phase2.py"


def _freeze_mod():
    spec = importlib.util.spec_from_file_location("freeze_phase2", _FREEZE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def study(tmp_path, monkeypatch, capsys):
    """Fictional files, a freeze record drawn from them, and a FROZEN protocol carrying it."""
    rows = make_sdr_rows(60, seed=5)
    sdr = tmp_path / "SDR-2026.csv"
    write_sdr_csv(rows, sdr)
    with sdr.open(newline="", encoding="utf-8") as f:
        marks = [(r["RegistryNNumber"], r["AircraftSerialNumber"]) for r in csv.DictReader(f)]
    master_rows = [master_row(n, s, "0010101", "1990") for n, s in marks[:50]]
    for i in range(4):  # a few serial disagreements to mine
        master_rows[i] = master_row(marks[i][0], f"OTHER{i}SERIAL", "0010101", "1990")
    master = tmp_path / "MASTER.txt"
    master.write_text(MASTER_HEADER + "\n" + "\n".join(master_rows) + "\n")
    ref = tmp_path / "ACFTREF.txt"
    ref.write_text(ACFTREF)
    items_dir = tmp_path / "data" / "phase2"
    monkeypatch.setattr(phase2, "MIN_SETTLED_AIRCRAFT", 1)
    freeze, items = tmp_path / "freeze.json", items_dir / "items.json"
    args = ["--master", str(master), "--acftref", str(ref), "--sdr", str(sdr),
            "--out", str(freeze), "--items", str(items)]  # fmt: skip
    assert _freeze_mod().main(args, items_dir=items_dir) == 0
    rec = json.loads(freeze.read_text())
    protocol = tmp_path / "PREREG.md"
    protocol.write_text(f"**Status: FROZEN 2026-11-01.**\n\n{rec['rules']['fingerprint']}\n"
                        f"{rec['item_list_sha256']}\n")  # fmt: skip
    monkeypatch.setattr(live_phase2, "PILOT_JOBS", 2)
    capsys.readouterr()
    base = ["--master", str(master), "--acftref", str(ref), "--sdr", str(sdr),
            "--items", str(items), "--freeze", str(freeze), "--protocol", str(protocol),
            "--runs-dir", str(tmp_path / "runs")]  # fmt: skip
    return {"base": base, "rec": rec, "tmp": tmp_path, "master": master, "items": items,
            "protocol": protocol, "runs": tmp_path / "runs"}  # fmt: skip


class _Answerer:
    """A fake live client: answers at once with a fixed reply and a known token count."""

    def __init__(self, fail_after: int | None = None):
        self.calls, self.fail_after = 0, fail_after

    def chat(self, request):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise QuotaExhausted("429")
        reply = json.dumps({"status": "ok", "answer": "X", "flags": []})
        return ChatResponse("muse-served", reply, 1000, 100)


def _live(monkeypatch, client):
    monkeypatch.setattr(live_phase2.clients, "make_client", lambda row, key: client)
    return {"OPENROUTER_API_KEY": "k", "XAI_API_KEY": "k"}


def _run(study, *extra, environ=None, today=None):
    p = ["run", "--model", "muse-spark-open", *study["base"], *extra]
    ns = _parse(p)  # parse through main's parser, then call run() with a fake env
    return live_phase2.run(ns, environ=environ or {}, today=today)


def _parse(argv):
    captured = {}

    def fake_run(args, environ=None, today=None):
        captured["args"] = args
        return 0

    real = live_phase2.run
    live_phase2.run = fake_run
    try:
        live_phase2.main(argv)
    finally:
        live_phase2.run = real
    return captured["args"]


def test_dry_run_checks_the_real_files_and_spends_nothing(study, capsys):
    assert _run(study, "--arm", "scored", "--budget-usd", "5", "--dry-run") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] and out["halted"] is None and out["left"] == 0
    assert out["ran"] == 2 * study["rec"]["tasks"] and out["session_usd"] == 0
    assert not study["runs"].exists()  # a dry run writes only to a throwaway directory


def test_pilot_then_scored_then_report(study, monkeypatch, capsys):
    env = _live(monkeypatch, _Answerer())
    confirm = ["--confirm", live_phase2.CONFIRM_PHRASE]
    assert _run(study, "--arm", "scored", "--budget-usd", "5", *confirm, environ=env) == 2
    assert "pilot has 0 of 2" in capsys.readouterr().err
    assert _run(study, "--arm", "pilot", "--budget-usd", "2", *confirm, environ=env) == 0
    pilot = json.loads(capsys.readouterr().out)
    assert pilot["ran"] == 2 and pilot["session_usd"] > 0
    assert _run(study, "--arm", "scored", "--budget-usd", "20", *confirm, environ=env) == 0
    scored = json.loads(capsys.readouterr().out)
    assert scored["ran"] == 2 * study["rec"]["tasks"] and scored["left"] == 0
    # resumable: the same command again runs nothing
    assert _run(study, "--arm", "scored", "--budget-usd", "1", *confirm, environ=env) == 0
    assert json.loads(capsys.readouterr().out)["ran"] == 0
    rep_path = study["tmp"] / "rep.json"
    assert live_phase2.main(["report", "--runs-dir", str(study["runs"]),
                             "--out", str(rep_path)]) == 0  # fmt: skip
    rep = json.loads(rep_path.read_text())
    muse = rep["models"]["muse-spark-open"]
    assert muse["conditions"]["baseline"]["runs"] == study["rec"]["tasks"]
    assert muse["rules_as_gate"]["balanced_accuracy"] == 1.0
    want = 1000 * 1.25 / 1e6 + 100 * 4.25 / 1e6
    assert rep["spend_usd"] == pytest.approx(want * (2 + 2 * study["rec"]["tasks"]), abs=1e-3)
    assert "X" not in json.dumps(muse["conditions"]["baseline"]["summary"])
    capsys.readouterr()


def test_budget_and_quota_halt_resumably(study, monkeypatch, capsys):
    confirm = ["--confirm", live_phase2.CONFIRM_PHRASE]
    env = _live(monkeypatch, _Answerer())
    assert _run(study, "--arm", "pilot", "--budget-usd", "0.01", *confirm, environ=env) == 3
    assert json.loads(capsys.readouterr().out)["halted"].startswith("budget")
    env = _live(monkeypatch, _Answerer(fail_after=1))
    assert _run(study, "--arm", "pilot", "--budget-usd", "2", *confirm, environ=env) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["ran"] == 1 and out["halted"].startswith("quota")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("no-confirm", "needs --confirm"),
        ("draft", "is not FROZEN"),
        ("protocol-missing-hash", "does not carry"),
        ("protocol-no-status", "no status line"),
        ("fingerprint", "mining.py changed"),
        ("input", "master file is not the one"),
        ("items", "does not rebuild"),
        ("over-model-cap", "exceeds its"),
        ("zero-budget", "must be positive"),
        ("no-key", "OPENROUTER_API_KEY is not set"),
    ],
)
def test_refusals_spend_nothing(study, monkeypatch, capsys, change, message):
    confirm = ["--confirm", live_phase2.CONFIRM_PHRASE]
    budget, env = "2", {"OPENROUTER_API_KEY": "k"}
    monkeypatch.setattr(live_phase2.clients, "make_client", lambda row, key: _Answerer())
    if change == "no-confirm":
        confirm = []
    elif change == "draft":
        study["protocol"].write_text(
            study["protocol"].read_text().replace("FROZEN 2026-11-01", "DRAFT")
        )
    elif change == "protocol-missing-hash":
        study["protocol"].write_text("**Status: FROZEN 2026-11-01.**\n")
    elif change == "protocol-no-status":
        study["protocol"].write_text("nothing here\n")
    elif change == "fingerprint":
        monkeypatch.setattr(mining, "fingerprint", lambda: "0" * 64)
    elif change == "input":
        study["master"].write_text(study["master"].read_text() + "\n")
    elif change == "items":
        raw = json.loads(study["items"].read_text())
        raw["tasks"] = raw["tasks"][1:]
        study["items"].write_text(json.dumps(raw))
    elif change == "over-model-cap":
        budget = "27.5"
    elif change == "zero-budget":
        budget = "0"
    elif change == "no-key":
        env = {}
    assert _run(study, "--arm", "pilot", "--budget-usd", budget, *confirm, environ=env) == 2
    assert message in capsys.readouterr().err
    assert not (study["runs"] / "muse-spark-open").exists()


def test_the_study_cap_counts_both_models(study, capsys):
    study["runs"].mkdir(parents=True)
    spend = study["runs"] / "spend.jsonl"
    spend.write_text(json.dumps({"run_id": "r", "model": "grok-4.7", "arm": "scored",
                                 "cost_usd": 37.9, "at": "x"}) + "\n")  # fmt: skip
    live_phase2.check_budget(spend, "muse-spark-open", 27.0)
    spend.write_text(json.dumps({"run_id": "r", "model": "grok-4.7", "arm": "scored",
                                 "cost_usd": 38.5, "at": "x"}) + "\n")  # fmt: skip
    with pytest.raises(live_phase2.Refused, match="study's"):
        live_phase2.check_budget(spend, "muse-spark-open", 27.0)


def test_unknown_model_price_and_caps_are_consistent(study, capsys, monkeypatch):
    ns = _parse(["run", "--model", "muse-spark-open", "--arm", "pilot", "--budget-usd", "1",
                 "--dry-run", *study["base"]])  # fmt: skip
    ns.model = "nope"
    assert live_phase2.run(ns) == 2 and "unknown model" in capsys.readouterr().err
    ns.model = "muse-spark-open"
    expired = clients.Pricing(1.25, 4.25, "test", valid_until=date(2026, 1, 1))
    row = clients.phase2_row("muse-spark-open")
    monkeypatch.setattr(
        live_phase2.clients,
        "phase2_row",
        lambda label: clients.ModelRow(
            row.slot, row.stage, row.provider, row.config, expired, row.key_env
        ),
    )
    assert live_phase2.run(ns, today=date(2026, 11, 1)) == 2
    assert "no longer in force" in capsys.readouterr().err
    assert set(live_phase2.MODEL_CAPS) == {r.config.label for r in clients.PHASE2_MODELS}
    assert live_phase2.estimate(phase2.Condition.PROMPT_FIX, row) > live_phase2.estimate(
        phase2.Condition.BASELINE, row
    )


def test_pilot_jobs_split_conditions_and_scored_covers_both(study):
    rec = study["rec"]
    raw = json.loads(study["items"].read_text())
    tasks = phase2.build_tasks(mining.Item(**t["item"]) for t in raw["tasks"])
    assert len(tasks) == rec["tasks"]
    pilot = live_phase2.jobs_for(tasks, "pilot")
    assert [c.value for _, c in pilot] == ["baseline", "prompt_fix"]
    scored = live_phase2.jobs_for(tasks, "scored")
    assert len(scored) == 2 * len(tasks) and len({(t.task_id, c) for t, c in scored}) == len(scored)


def test_a_bad_pilot_blocks_the_scored_run(study, monkeypatch, capsys):
    env = _live(monkeypatch, _Answerer())
    confirm = ["--confirm", live_phase2.CONFIRM_PHRASE]
    assert _run(study, "--arm", "pilot", "--budget-usd", "2", *confirm, environ=env) == 0
    pilot = study["runs"] / "muse-spark-open" / "pilot.jsonl"
    recs = [json.loads(line) for line in pilot.read_text().splitlines()]
    recs[0]["exclusion"] = "RUN_ERROR"
    pilot.write_text("".join(json.dumps(r) + "\n" for r in recs))
    capsys.readouterr()
    assert _run(study, "--arm", "scored", "--budget-usd", "5", *confirm, environ=env) == 2
    assert "RUN_ERROR or an unknown cost" in capsys.readouterr().err


def _jobs(study):
    raw = json.loads(study["items"].read_text())
    tasks = phase2.build_tasks(mining.Item(**t["item"]) for t in raw["tasks"])
    return live_phase2.jobs_for(tasks, "pilot")


def test_a_quota_halt_books_what_was_paid(study, monkeypatch):
    def quota(*a, **k):
        exc = QuotaExhausted("429 after one paid turn")
        exc.paid_usd = 0.25
        raise exc

    monkeypatch.setattr(live_phase2.phase2, "run_item", quota)
    row, spend = clients.phase2_row("muse-spark-open"), study["tmp"] / "spend.jsonl"
    out = live_phase2.execute(_jobs(study), row, "pilot", None, lambda t: None,
                              study["tmp"] / "o.jsonl", spend, 2.0)  # fmt: skip
    assert out["halted"].startswith("quota") and out["session_usd"] == 0.25
    assert live_phase2.spent(spend, "muse-spark-open") == 0.25


def test_an_unknown_cost_is_booked_at_the_estimate_and_halts(study, monkeypatch):
    def unpriced(task, sources, client, model, cond, *, run_id, pricing):
        return phase2.RunRecord(**{**_any_record(), "run_id": run_id, "task_id": task.task_id,
                                   "condition": cond.value, "cost_usd": None})  # fmt: skip

    monkeypatch.setattr(live_phase2.phase2, "run_item", unpriced)
    row, spend = clients.phase2_row("muse-spark-open"), study["tmp"] / "spend.jsonl"
    jobs = _jobs(study)
    out = live_phase2.execute(jobs, row, "pilot", None, lambda t: None,
                              study["tmp"] / "o.jsonl", spend, 2.0)  # fmt: skip
    assert out["ran"] == 1 and out["halted"].startswith("a run's cost is unknown")
    assert live_phase2.spent(spend) == pytest.approx(live_phase2.estimate(jobs[0][1], row))


def _any_record():
    import dataclasses

    fields = {f.name: None for f in dataclasses.fields(phase2.RunRecord)}
    return fields | {"served_models": (), "tool_calls": (), "input_tokens": 0,
                     "output_tokens": 0, "latency_s": 0.0, "turns": 1}  # fmt: skip


class _Refuser:
    """A client whose every call fails at the HTTP layer, as a bad key does."""

    def __init__(self):
        self.calls = 0

    def chat(self, request):
        self.calls += 1
        raise XaiError("HTTP 401")


def test_a_bad_key_records_nothing_and_a_fixed_key_resumes(study, monkeypatch, capsys):
    confirm = ["--confirm", live_phase2.CONFIRM_PHRASE]
    bad = _Refuser()
    env = _live(monkeypatch, bad)
    assert _run(study, "--arm", "pilot", "--budget-usd", "2", *confirm, environ=env) == 3
    out = json.loads(capsys.readouterr().out)
    assert bad.calls == 1 and out["ran"] == 0 and "HTTP 401" in out["halted"]
    pilot = study["runs"] / "muse-spark-open" / "pilot.jsonl"
    assert not pilot.exists() and live_phase2.spent(study["runs"] / "spend.jsonl") == 0
    env = _live(monkeypatch, _Answerer())
    assert _run(study, "--arm", "pilot", "--budget-usd", "2", *confirm, environ=env) == 0
    assert json.loads(capsys.readouterr().out)["ran"] == live_phase2.PILOT_JOBS


def test_an_error_after_a_good_run_is_recorded_as_a_run_error(study, monkeypatch, capsys):
    class _Flaky(_Answerer):
        def chat(self, request):
            if self.calls >= 1:
                raise XaiError("HTTP 500")
            return super().chat(request)

    confirm = ["--confirm", live_phase2.CONFIRM_PHRASE]
    env = _live(monkeypatch, _Flaky())
    assert _run(study, "--arm", "pilot", "--budget-usd", "2", *confirm, environ=env) == 0
    recs = [
        json.loads(x)
        for x in (study["runs"] / "muse-spark-open" / "pilot.jsonl").read_text().splitlines()
    ]
    assert len(recs) == live_phase2.PILOT_JOBS and recs[-1]["exclusion"] == "RUN_ERROR"
    capsys.readouterr()
