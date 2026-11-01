"""v2 live driver: caps, shards, protocol status, pilot gate, ledger merge. No network, no key."""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

import pytest
import support as sp

from tripwire import clients, live_v2, runner, tasks_v2
from tripwire.live_eval import CONFIRM_PHRASE, DryRunClient
from tripwire.prompts import Condition

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
TODAY = date(2026, 11, 2)
ROOT = Path(__file__).resolve().parents[1]
LABELS = ("grok-4.7", "claude-sonnet-5-5", "gemini-3.8-flash", "gpt-oss-120b")
M1, M2, M3, M4 = (clients.v2_row(x) for x in LABELS)
DRAFT = "# v2\n\n**Status: DRAFT, not frozen.** text\n"
FROZEN = "# v2\n\n**Status: FROZEN 2026-11-02**\n"


def _arm_usd(row, arm, shard=(1, 1)):
    jobs = live_v2.shard_jobs(live_v2.arm_jobs(row, arm), *shard)
    return sum(live_v2.estimate(j, row.pricing) for j in jobs)


def test_caps_hold_every_arm_and_every_shard_and_fit_the_ceiling():
    assert sum(live_v2.BUCKET_CAPS.values()) <= live_v2.HARD_CEILING_USD
    for row in (M1, M2, M3, M4):
        cap = live_v2.BUCKET_CAPS[row.slot]
        assert len(live_v2.scored_jobs(row)) == 960 and _arm_usd(row, "scored") <= cap
        for n in (4, 6, 8):  # the shard counts the workflow offers
            assert max(_arm_usd(row, "scored", (i, n)) for i in range(1, n + 1)) <= cap / n
    pilots = sum(_arm_usd(r, "pilot") for r in (M2, M3, M4)) + _arm_usd(M1, "bridge")
    assert pilots <= live_v2.BUCKET_CAPS[live_v2.PILOTS]
    assert len(live_v2.arm_jobs(M4, "pilot")) == 20 and len(live_v2.arm_jobs(M1, "bridge")) == 24
    assert {j.condition for j in live_v2.arm_jobs(M1, "bridge")} == {Condition.GATE}


def test_shards_are_stable_disjoint_covering_and_balanced():
    jobs = live_v2.scored_jobs(M1)
    parts = [live_v2.shard_jobs(jobs, i, 6) for i in range(1, 7)]
    ids = [j.job_id for p in parts for j in p]
    assert sorted(ids) == sorted(j.job_id for j in jobs) and len(set(ids)) == 960
    for p in parts:
        assert sum(j.condition is Condition.BASELINE for j in p) == 80 and len(p) == 160
    assert live_v2.shard_jobs(jobs, 2, 6) == parts[1]
    assert live_v2.shard_jobs(jobs, 1, 1) == jobs
    for bad in ("0/3", "4/3", "1", "a/b", "1/0"):
        with pytest.raises(runner.RunnerRefused):
            live_v2.parse_shard(bad)


def test_protocol_status_line():
    assert live_v2.protocol_status(DRAFT) == ("DRAFT", None)
    assert live_v2.protocol_status(FROZEN) == ("FROZEN", date(2026, 11, 2))
    assert live_v2.protocol_status((ROOT / live_v2.PROTOCOL).read_text())[0] == "DRAFT"
    for bad in ("no status\n", DRAFT + FROZEN):
        with pytest.raises(runner.RunnerRefused, match="status line"):
            live_v2.protocol_status(bad)


def test_the_committed_v2_ledger_is_an_empty_header():
    path = ROOT / live_v2.LEDGER
    assert runner.read_spend(path) == 0 and len(path.read_text().splitlines()) == 1
    assert live_v2.spend_by_bucket(path) == dict.fromkeys(live_v2.BUCKET_CAPS, 0.0)


# --- the driver ------------------------------------------------------------------------------


def _commit(root, text):
    (root / live_v2.PROTOCOL).write_text(text)
    subprocess.run([*GIT, "-C", str(root), "add", "."], check=True)
    subprocess.run([*GIT, "-C", str(root), "commit", "-qm", "p"], check=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    subprocess.run([*GIT, "-C", str(root), "init", "-q"], check=True)
    _commit(root, DRAFT)
    return root


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "spend_ledger_v2.jsonl"
    runner.init_spend_ledger(path, sp.CLOCK)
    return path


def argv(repo, ledger, out, *extra, **kw):
    base = {"model": "grok-4.7", "stage": "1", "budget-usd": "14", "ledger-rows": "0",
            "confirm": CONFIRM_PHRASE}  # fmt: skip
    base.update({k.replace("_", "-"): v for k, v in kw.items()})
    out_args = ["run", "--repo", str(repo), "--spend-ledger", str(ledger), "--out", str(out)]
    for k, v in base.items():
        out_args += [f"--{k}", str(v)]
    return [*out_args, *extra]


def run(*a, env=None, today=TODAY, **kw):
    return live_v2.main(argv(*a, **kw), env=env or {}, today=today)


def test_dry_run_tags_every_row_and_leaves_the_real_ledger_alone(repo, ledger, tmp_path):
    out, before = tmp_path / "out", ledger.read_text()
    assert run(repo, ledger, out, "--dry-run", "--max-jobs", "3") == 0
    assert ledger.read_text() == before
    rows = [json.loads(x) for x in (out / "dry_run_spend_ledger_v2.jsonl").read_text().splitlines()]
    assert len(rows) == 4 and all(r["bucket"] == "M1" and r["study"] == "v2" for r in rows[1:])
    assert {r["shard"] for r in rows[1:]} == {"1/1"} and {r["arm"] for r in rows[1:]} == {"scored"}
    recs = [json.loads(x) for x in (out / "receipt.jsonl").read_text().splitlines()]
    assert {r["task_set"] for r in recs} == {"v2"} and all(r["shadow"] for r in recs)
    meta = json.loads((out / "meta.json").read_text())
    assert (meta["study"], meta["arm"], meta["slot"], meta["pilot"]) == (
        "v2",
        "scored",
        "M1",
        False,
    )
    # a resume into the same directory must be the same arm, row and shard
    assert run(repo, ledger, out, "--dry-run", "--max-jobs", "1", shard="1/6") == 2


def test_bridge_runs_the_live_gate_without_shadow_gates(repo, ledger, tmp_path):
    out = tmp_path / "bridge"
    assert run(repo, ledger, out, "--dry-run", "--max-jobs", "2", arm="bridge", budget_usd=4) == 0
    recs = [json.loads(x) for x in (out / "receipt.jsonl").read_text().splitlines()]
    assert {r["condition"] for r in recs} == {"gate"} and not any(r["shadow"] for r in recs)


@pytest.mark.parametrize(
    ("extra", "kw", "message"),
    [
        ((), {"confirm": "yes"}, "confirm"),
        ((), {"stage": "2"}, "stage 1"),
        ((), {"model": "gpt-4o"}, "no v2 model row"),
        ((), {"arm": "pilot"}, "M1 has no pilot"),
        ((), {"model": "gpt-oss-120b", "arm": "bridge", "budget_usd": 1}, "M1 only"),
        ((), {"model": "gpt-oss-120b", "arm": "pilot", "shard": "1/2"}, "only the scored"),
        (("--max-jobs", "1"), {}, "dry-run only"),
        ((), {}, "FROZEN"),  # the DRAFT protocol refuses a live run
        (("--dry-run",), {"ledger_rows": "1"}, "expected 1"),
        (("--dry-run",), {"budget_usd": "87.01"}, "must be in"),
        (("--dry-run",), {"budget_usd": "14.6", "shard": "1/6"}, "must be in"),
        (("--dry-run",), {"budget_usd": "0"}, "must be in"),
        (("--dry-run",), {"model": "gpt-oss-120b", "budget_usd": 1}, "--pilot-from"),
        (("--dry-run",), {"model": "gemini-3.8-flash", "stage": 2, "budget_usd": 1}, "pilot"),
    ],
)
def test_refusals(repo, ledger, tmp_path, capsys, extra, kw, message):
    assert run(repo, ledger, tmp_path / "out", *extra, **kw) == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "out" / "receipt.jsonl").exists()


def test_expired_prices_refuse_even_a_dry_run(repo, ledger, tmp_path, capsys):
    code = run(repo, ledger, tmp_path / "o", "--dry-run", model="gemini-3.8-flash", stage=2,
               budget_usd=1, today=date(2027, 1, 1))  # fmt: skip
    assert code == 2 and "expired" in capsys.readouterr().err


def test_a_frozen_status_dated_after_today_refuses(repo, ledger, tmp_path, capsys):
    _commit(repo, FROZEN)
    code = run(repo, ledger, tmp_path / "o", "--no-prior-paid-run", today=date(2026, 11, 1))
    assert code == 2 and "FROZEN" in capsys.readouterr().err


def test_live_needs_a_prior_ledger_flag_and_the_rows_own_key(repo, ledger, tmp_path, capsys):
    _commit(repo, FROZEN)
    assert run(repo, ledger, tmp_path / "o") == 2
    assert "--prior-ledger" in capsys.readouterr().err
    assert run(repo, ledger, tmp_path / "o", "--no-prior-paid-run",
               env={"TOGETHER_API_KEY": "k"}) == 2  # fmt: skip
    assert "XAI_API_KEY is not set" in capsys.readouterr().err


def test_an_untagged_spend_row_fails_closed(repo, ledger, tmp_path, capsys):
    with ledger.open("a") as fh:
        fh.write(json.dumps({"kind": "spend", "run_id": "x", "cost_usd": 0.1}) + "\n")
    assert run(repo, ledger, tmp_path / "o", "--dry-run", ledger_rows="1") == 2
    assert "known bucket" in capsys.readouterr().err


def test_a_live_shard_gets_its_key_and_an_equal_share(repo, ledger, tmp_path, monkeypatch):
    _commit(repo, FROZEN)
    built = []

    def fake_make_client(row, key):
        built.append((row.slot, key))
        return DryRunClient()

    monkeypatch.setattr(live_v2.clients, "make_client", fake_make_client)
    monkeypatch.setattr(live_v2, "WIRE_CONFIRMED", True)
    out = tmp_path / "s3"
    env = {"XAI_API_KEY": "sk-x", "TOGETHER_API_KEY": "sk-t"}
    code = run(repo, ledger, out, "--no-prior-paid-run", env=env, shard="3/6", budget_usd=14.5)
    assert code == 0 and built == [("M1", "sk-x")]
    jobs = live_v2.shard_jobs(live_v2.scored_jobs(M1), 3, 6)
    recs = [json.loads(x) for x in (out / "receipt.jsonl").read_text().splitlines()]
    assert [r["run_id"] for r in recs] == [j.job_id for j in jobs]
    result = json.loads((out / "result.json").read_text())
    assert result["ceiling_usd"] == pytest.approx(14.5) and result["shard"] == "3/6"
    rows = live_v2.spend_by_bucket(ledger)  # a live run appends to the real (shard's) ledger
    assert rows["M1"] == pytest.approx(runner.read_spend(ledger)) and rows["M1"] > 0
    # the next shard from this ledger only gets a share of what is left
    left = (live_v2.BUCKET_CAPS["M1"] - rows["M1"]) / 6
    assert live_v2.room_usd(ledger, "M1", 6) == pytest.approx(left)


def test_wire_unconfirmed_xai_refuses(repo, ledger, tmp_path, monkeypatch, capsys):
    _commit(repo, FROZEN)
    monkeypatch.setattr(live_v2, "WIRE_CONFIRMED", False)
    assert run(repo, ledger, tmp_path / "o", "--no-prior-paid-run", env={"XAI_API_KEY": "k"}) == 2
    assert "WIRE_CONFIRMED" in capsys.readouterr().err


# --- the pilot gate --------------------------------------------------------------------------


def test_a_dry_pilot_then_passes_the_gate_for_the_scored_run(repo, ledger, tmp_path):
    pilot = tmp_path / "pilot"
    code = run(repo, ledger, pilot, "--dry-run", model="gpt-oss-120b", arm="pilot", budget_usd=1)
    assert code == 0
    assert json.loads((pilot / "meta.json").read_text())["pilot"] is True
    out = tmp_path / "scored"
    code = run(repo, ledger, out, "--dry-run", "--max-jobs", "2", "--pilot-from", str(pilot),
               model="gpt-oss-120b", budget_usd=1.3)  # fmt: skip
    assert code == 0


def _pilot_dir(tmp_path, row, n=20, cost=None, exclusion=None, label=None, **meta):
    d = tmp_path / "p"
    d.mkdir(exist_ok=True)
    m = {"arm": "pilot", "slot": row.slot, "protocol_sha256": "abc", **meta}
    (d / "meta.json").write_text(json.dumps(m))
    jobs = live_v2.arm_jobs(row, "pilot")[:n]
    lines = []
    for j in jobs:
        plain = live_v2.condition_estimate(j.condition, row.pricing, 1.0)
        rec = {"run_id": j.job_id, "condition": j.condition.value, "exclusion": exclusion,
               "model_label": label or row.config.label,
               "cost_usd": plain * cost if cost is not None else None}  # fmt: skip
        lines.append(json.dumps(rec))
    (d / "receipt.jsonl").write_text("\n".join(lines) + "\n")
    return d


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"n": 19, "cost": 1.0}, "19 records"),
        ({"cost": 1.0, "exclusion": "RUN_ERROR"}, "RUN_ERROR"),
        ({"cost": 1.0, "label": "other"}, "another model"),
        ({}, "no cost"),
        ({"cost": 1.51}, "owner decision"),
        ({"cost": 1.0, "protocol_sha256": "def"}, "different protocol"),
        ({"cost": 1.0, "arm": "scored"}, "not a M4 pilot"),
    ],
)
def test_the_pilot_gate_fails_closed(tmp_path, kw, message):
    with pytest.raises(runner.RunnerRefused, match=message):
        live_v2.check_pilot(_pilot_dir(tmp_path, M4, **kw), M4, "abc")


def test_the_pilot_gate_passes_at_the_ratio_and_needs_files(tmp_path):
    assert live_v2.check_pilot(_pilot_dir(tmp_path, M2, cost=1.5), M2, "abc") == pytest.approx(1.5)
    with pytest.raises(runner.RunnerRefused, match="no pilot"):
        live_v2.check_pilot(tmp_path / "missing", M2, "abc")


# --- merging shard ledgers -------------------------------------------------------------------


def _row(run_id, cost, bucket="M1"):
    return json.dumps({"kind": "spend", "run_id": run_id, "cost_usd": cost, "study": "v2",
                       "bucket": bucket, "timestamp": "t"}, sort_keys=True)  # fmt: skip


def _shard(base, path, *rows):
    path.write_text(base.read_text() + "".join(r + "\n" for r in rows))
    return path


def test_merge_joins_shards_in_order_and_reports_totals(ledger, tmp_path, capsys):
    a = _shard(ledger, tmp_path / "a.jsonl", _row("a1", 1.0), _row("a2", 2.0))
    b = _shard(ledger, tmp_path / "b.jsonl", _row("b1", 0.5, "M4"))
    out = tmp_path / "merged.jsonl"
    code = live_v2.main(["merge", "--base", str(ledger), "--out", str(out), str(a), str(b)])
    assert code == 0
    assert out.read_text().splitlines()[1:] == [
        _row("a1", 1.0),
        _row("a2", 2.0),
        _row("b1", 0.5, "M4"),
    ]
    assert (
        live_v2.spend_by_bucket(out)["M1"] == 3.0
        and "M4: 0.5000 of 8.00" in capsys.readouterr().out
    )


def test_merge_refuses_a_shard_from_another_base_or_a_repeated_row(ledger, tmp_path):
    a = _shard(ledger, tmp_path / "a.jsonl", _row("a1", 1.0))
    other = tmp_path / "other.jsonl"
    runner.init_spend_ledger(other, lambda: sp.NOW.replace(year=2027))
    with pytest.raises(runner.RunnerRefused, match="extend"):
        live_v2.merge_ledgers(ledger, [_shard(other, tmp_path / "b.jsonl", _row("b", 1.0))])
    with pytest.raises(runner.RunnerRefused, match="repeats"):
        live_v2.merge_ledgers(ledger, [a, a])
    out = tmp_path / "m.jsonl"
    assert live_v2.main(["merge", "--base", str(ledger), "--out", str(out), str(a), str(a)]) == 2


def test_merge_writes_an_over_cap_ledger_but_exits_nonzero(ledger, tmp_path, capsys):
    a = _shard(ledger, tmp_path / "a.jsonl", _row("a1", 9.0, "M4"))
    out = tmp_path / "m.jsonl"
    assert live_v2.main(["merge", "--base", str(ledger), "--out", str(out), str(a)]) == 1
    assert runner.read_spend(out) == 9.0 and "OVER CAP" in capsys.readouterr().err


def test_the_v2_task_store_is_what_the_driver_runs():
    assert {t.task_id for t in tasks_v2.default_tasks()} >= {j.plan.task_id for j in
                                                            live_v2.scored_jobs(M1)}  # fmt: skip
