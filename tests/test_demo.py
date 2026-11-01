"""M8 side-by-side demo: fixed slot, all three conditions, transcripts kept, log round trip."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from support import CLOCK, MODEL, PRICE, agent_script, truth_json

from tripwire import demo, harness, live_eval, runner
from tripwire.prompts import Condition
from tripwire.xai import ExecPath, ScriptedClient

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "runs" / "spend_ledger.jsonl"


def _spend_count() -> int:
    return sum(1 for x in LEDGER.read_text().splitlines()[1:] if json.loads(x)["kind"] == "spend")


def test_jobs_are_the_fixed_slot_repeat_major_and_new_to_the_ledger():
    js = demo.jobs()
    assert len(js) == demo.REPEATS * len(demo.CONDITIONS) == 9
    assert {j.plan for j in js} == {demo.SLOT}
    assert [j.condition for j in js[:3]] == list(demo.CONDITIONS)
    assert [j.repeat for j in js] == [1, 1, 1, 2, 2, 2, 3, 3, 3]
    assert all(j.job_id.startswith(demo.PREFIX) for j in js)
    ledger_ids = {json.loads(x).get("run_id") for x in LEDGER.read_text().splitlines()[1:]}
    assert not ledger_ids & {j.job_id for j in js}
    assert demo.FAULT in demo._task().faults


def _run(condition: Condition, final: str) -> tuple[harness.RunRecord, demo.RecordingClient]:
    task = demo._task()
    rec_client = demo.RecordingClient(
        ScriptedClient(agent_script(task, ExecPath.CLIENT_SIDE, final))
    )
    rec = harness.run_one(
        demo.SLOT, task, MODEL, ExecPath.CLIENT_SIDE, condition, rec_client,
        run_id=f"{demo.SLOT.run_id}.x.{condition.value}.k1", clock=CLOCK, pricing=PRICE,
    )  # fmt: skip
    return rec, rec_client


def test_transcript_shows_the_served_fault_and_the_gate_blocks_it():
    truthful = truth_json(demo._task())  # an agent that answers the clean truth anyway
    base, base_client = _run(Condition.BASELINE, truthful)
    gated, gate_client = _run(Condition.GATE, truthful)
    records = [json.loads(base.to_json()), json.loads(gated.to_json())]
    pairs = demo.pair_transcripts(records, base_client.segments + gate_client.segments)
    served = demo._served(pairs[0])
    assert "12.446" in served and '"in"' in served  # 0.49 in converted to mm, label unchanged
    assert pairs[0]["reply"] == truthful and len(pairs[0]["messages"]) == 3
    assert all("output_items" not in m for p in pairs for m in p["messages"])
    assert records[1]["gate"]["blocked"] and records[1]["adjudication"]["correct"]
    page = demo.render(records, pairs)
    assert "blocked (F1)" in page and "12.446" in page and "A demo, not a result" in page


def test_pairing_fails_closed():
    rec, client = _run(Condition.BASELINE, truth_json(demo._task()))
    row = json.loads(rec.to_json())
    with pytest.raises(demo.DemoError, match="records but"):
        demo.pair_transcripts([row, row], client.segments)
    wrong = json.loads(rec.to_json()) | {"condition": "prompt_fix"}
    with pytest.raises(demo.DemoError, match="does not open with its own prompt"):
        demo.pair_transcripts([wrong], client.segments)


def test_emit_and_parse_log_round_trip(tmp_path):
    rec, client = _run(Condition.GATE, truth_json(demo._task()))
    rows = [json.loads(rec.to_json())]
    demo.write_jsonl(tmp_path / demo.RECEIPT_FILE, rows)
    demo.write_jsonl(tmp_path / demo.TRANSCRIPT_FILE, demo.pair_transcripts(rows, client.segments))
    spend = [{"kind": "spend", "run_id": rec.run_id, "cost_usd": 0.01, "timestamp": "t"}]
    buf = io.StringIO()
    demo.emit(tmp_path, spend, buf)
    log = "".join(f"2026-11-01T05:00:00.0000000Z {line}\n" for line in buf.getvalue().splitlines())
    blocks = demo.parse_log("noise before\n" + log + "noise after\n")
    assert blocks["receipt"] == rows and blocks["spend"] == spend
    with pytest.raises(demo.DemoError, match="no spend block"):
        demo.parse_log(log.split(f"{demo.MARK} BEGIN spend")[0])


def _args(tmp_path, *extra):
    return ["--condition-set", "demo-1", "--confirm", live_eval.CONFIRM_PHRASE,
            "--ledger-rows", str(_spend_count()), "--dry-run", "--out", str(tmp_path / "o"),
            "--repo", str(ROOT), "--spend-ledger", str(LEDGER), *extra]  # fmt: skip


def test_driver_refuses_over_cap_mixed_flags_and_a_reused_dir(tmp_path, capsys):
    assert live_eval.main(_args(tmp_path, "--budget-usd", "1.6")) == runner.ExitCode.REFUSED
    assert "demo-1 needs budget" in capsys.readouterr().err
    assert live_eval.main(_args(tmp_path, "--budget-usd", "1", "--pilot-jobs", "12")) == 2
    assert "demo-1 excludes" in capsys.readouterr().err
    (tmp_path / "o").mkdir()
    (tmp_path / "o" / demo.RECEIPT_FILE).write_text("")
    assert live_eval.main(_args(tmp_path, "--budget-usd", "1")) == 2
    assert "already holds a demo receipt" in capsys.readouterr().err


def test_driver_dry_run_runs_the_demo_and_prints_the_blocks(tmp_path, capsys):
    assert live_eval.main(_args(tmp_path, "--budget-usd", "1.5")) == 0
    blocks = demo.parse_log(capsys.readouterr().out)
    assert [r["run_id"] for r in blocks["receipt"]] == [j.job_id for j in demo.jobs()]
    assert len(blocks["transcripts"]) == 9 and len(blocks["spend"]) == 9
    meta = json.loads((tmp_path / "o" / "meta.json").read_text())
    assert meta["demo"] is True and meta["targets"] == "demo-1"
    page = tmp_path / "page.md"
    assert demo.main(["--render", str(tmp_path / "o"), "--out", str(page)]) == 0
    assert page.read_text().count("| k") == 9
