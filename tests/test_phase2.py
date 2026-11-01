"""Phase two read tools, questions and scorer on synthetic records. Scripted clients only."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest

from tests.test_mining import BASE, REG
from tripwire import mining
from tripwire import phase2 as p2
from tripwire.adjudicator import Status
from tripwire.prompts import PROMPT_FIX_TEXT
from tripwire.xai import (
    ChatResponse,
    ModelConfig,
    Pricing,
    QuotaExhausted,
    ScriptedClient,
    ToolCallRequest,
    XaiError,
)

MODEL = ModelConfig("scripted", "no-model", None, "test")
SOURCES = p2.Sources.build(
    [BASE, replace(BASE, control_number="X2", aircraft_serial_number="ASN9999")], REG
)


def item(rule="control", field="aircraft_serial_number", sdr="asn 1001", reg="ASN-1001",
         expected="agree"):  # fmt: skip
    return mining.Item(rule, BASE.control_number, "101", field, sdr, reg, expected)


def task(**kw):
    return p2.make_task(item(**kw))


MINED = task(rule="S1", sdr="ASN9999", expected="disagree")
CONTROL = task()
HOURS = task(field="aircraft_total_time", sdr="1000", reg="2005")


def v(status="ok", answer=None, flags=()):
    return p2.Verdict(Status(status), answer, tuple(flags))


# --- tools ----------------------------------------------------------------------------------


def test_get_sdr_serves_the_report_and_not_found_is_typed():
    rep = SOURCES.call("get_sdr", {"control_number": BASE.control_number})["report"]
    assert rep["aircraft_serial_number"] == "asn 1001" and rep["difficulty_date"] == "2026-01-02"
    assert rep["registry_n_number"] == "101"
    assert SOURCES.get_sdr("nope")["error"].startswith("NOT_FOUND")


def test_get_registration_joins_the_reference_and_fails_typed():
    reg = SOURCES.call("get_registration", {"n_number": "N101"})["registration"]
    assert reg["serial_number"] == "ASN-1001" and reg["manufacturer"] == "ACME AERO WORKS"
    assert reg["year_manufactured"] == 2005 and reg["certificate_issue_date"] == "2020-01-01"
    assert SOURCES.get_registration("303")["registration"]["certificate_issue_date"] is None
    assert SOURCES.get_registration("N404")["error"].startswith("AMBIGUOUS")
    assert SOURCES.get_registration("999")["error"].startswith("NOT_FOUND")
    no_ref = p2.Sources.build([], replace(REG, by_code={}))
    assert no_ref.get_registration("101")["registration"]["manufacturer"] is None


@pytest.mark.parametrize(
    ("name", "args", "prefix"),
    [
        ("drop_table", {}, "UNKNOWN_TOOL"),
        ("get_sdr", {}, "BAD_ARGS"),
        ("get_sdr", {"control_number": 5}, "BAD_ARGS"),
        ("get_registration", {"n_number": "101", "x": "y"}, "BAD_ARGS"),
    ],
)
def test_bad_calls_are_errors_the_model_sees(name, args, prefix):
    assert SOURCES.call(name, args)["error"].startswith(prefix)


def test_sources_refuse_duplicate_reports_and_specs_match_the_tools():
    with pytest.raises(p2.Phase2Error, match="twice"):
        p2.Sources.build([BASE, BASE], REG)
    specs = p2.tool_specs()
    assert tuple(s["function"]["name"] for s in specs) == p2.TOOLS
    assert all(s["function"]["parameters"]["additionalProperties"] is False for s in specs)


# --- questions ------------------------------------------------------------------------------


def test_questions_name_only_the_report_and_prompts_differ_only_by_the_warning():
    for field in p2.QUESTIONS:
        q = task(field=field).question
        assert BASE.control_number in q and "registr" not in q.lower()
    base = p2.prompt(CONTROL, p2.Condition.BASELINE)
    fix = p2.prompt(CONTROL, p2.Condition.PROMPT_FIX)
    assert PROMPT_FIX_TEXT not in base and PROMPT_FIX_TEXT in fix
    assert fix.replace(f"{PROMPT_FIX_TEXT}\n\n", "") == base
    assert p2.FLAG in base and "get_registration" in base


def test_task_ids_are_stable_and_bad_items_refused():
    assert CONTROL.task_id == task().task_id and CONTROL.task_id.startswith(p2.TASK_PREFIX)
    assert MINED.mined and not CONTROL.mined
    with pytest.raises(p2.Phase2Error, match="template"):
        task(field="part_name")
    with pytest.raises(p2.Phase2Error, match="agree or disagree"):
        task(expected="maybe")
    with pytest.raises(p2.Phase2Error, match="share"):
        p2.build_tasks([item(), item()])


def test_item_list_hash_ignores_order():
    a = p2.build_tasks([item(), item(field="aircraft_total_time", sdr="1000", reg="2005")])
    assert p2.item_list_sha256(a) == p2.item_list_sha256(list(reversed(a)))
    assert p2.item_list_sha256(a[:1]) != p2.item_list_sha256(a)


# --- verdicts -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        {"answer": 1},
        {"status": "ok", "value": 1},
        {"status": "fine"},
        {"status": "ok", "answer": True},
        {"status": "ok", "answer": [1]},
        {"status": "ok", "flags": "record_disagreement"},
        {"status": "ok", "flags": [1]},
        [],
    ],
)
def test_verdict_contract_is_strict(raw):
    with pytest.raises(p2.Phase2Error):
        p2.Verdict.from_dict(raw)


def test_extract_takes_the_last_valid_object():
    text = 'draft {"status": "ok", "answer": 1} then {"bad": 1} final {"status": "refused"}'
    assert p2.extract_verdict(text) == v("refused")
    assert p2.extract_verdict('{"status": "ok", "answer": 7}').answer == 7.0
    assert p2.extract_verdict("no json {oops") is None


# --- scoring --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "correct", "flagged"),
    [
        (v("refused", None, ["record_disagreement"]), True, True),
        (v("escalated", None, ["Record-Disagreement"]), True, True),
        (v("blocked", None, ["record disagreement"]), True, True),
        (v("ok", "ASN9999", ["record_disagreement"]), False, True),  # answered anyway
        (v("retried", None, ["record_disagreement"]), False, True),
        (v("refused", None, []), False, False),  # refused without saying why
        (v("ok", "ASN9999"), False, False),
        (None, False, False),
    ],
)
def test_mined_item_scoring(verdict, correct, flagged):
    s = p2.score(MINED, verdict)
    assert (s.correct, s.flagged, s.false_alarm) == (correct, flagged, False)
    assert s.no_verdict == (verdict is None)


@pytest.mark.parametrize(
    ("verdict", "correct", "alarm"),
    [
        (v("ok", "ASN-1001"), True, False),
        (v("ok", "asn1001"), True, False),
        (v("ok", "ASN-1002"), False, False),
        (v("ok", None), False, False),
        (v("ok", "ASN-1001", ["record_disagreement"]), False, True),
        (v("ok", "ASN-1001", ["looks odd"]), False, True),
        (v("refused", None, ["record_disagreement"]), False, True),
        (None, False, False),
    ],
)
def test_control_scoring(verdict, correct, alarm):
    s = p2.score(CONTROL, verdict)
    assert (s.correct, s.false_alarm) == (correct, alarm)


def test_answers_are_matched_per_field_and_attributed_to_a_source():
    assert p2.score(HOURS, v("ok", 1000.04)).correct
    assert not p2.score(HOURS, v("ok", 1000.2)).correct
    assert p2.score(HOURS, v("ok", "1000")).correct
    assert not p2.score(HOURS, v("ok", "about a thousand")).correct
    when = task(field="difficulty_date", sdr="2026-01-02", reg="2027")
    assert p2.score(when, v("ok", " 2026-01-02")).correct
    assert not p2.score(when, v("ok", "Jan 2")).correct
    make = task(rule="M1", field="aircraft_make", sdr="ZENITH", reg="ACME AERO WORKS",
                expected="disagree")  # fmt: skip
    assert p2.answered_from(make, v("ok", "Acme Aero")) == "registry"
    assert p2.answered_from(make, v("ok", "Zenith")) == "sdr"
    assert p2.answered_from(make, v("ok", "Zenith and Acme")) == "both"
    assert p2.answered_from(MINED, v("ok", "ASN-1001")) == "registry"
    assert p2.answered_from(MINED, v("ok", "nonsense")) == "neither"
    assert p2.answered_from(MINED, v("ok", "")) == "neither"
    assert p2.answered_from(HOURS, v("ok", 2005)) == "neither"  # a year is not hours
    assert p2.answered_from(MINED, None) is None
    assert not p2._matches("aircraft_make", "X", "")


def test_reference_is_right_by_construction():
    for t in (MINED, CONTROL, HOURS):
        assert p2.score(t, p2.reference_verdict(t)).correct
    assert p2.reference_verdict(HOURS).answer == 1000.0


def test_summary_balanced_accuracy_and_ci():
    scores = [p2.score(MINED, v("refused", None, [p2.FLAG]))] * 3 + [
        p2.score(CONTROL, v("ok", "ASN-1001")),
        p2.score(CONTROL, v("refused", None, [p2.FLAG])),
    ]
    out = p2.summarize(scores)
    assert out["mined"]["correct"] == 1.0 and out["controls"]["correct"] == 0.5
    assert out["balanced_accuracy"] == 0.75 and out["controls"]["false_alarm"] == 0.5
    lo, hi = out["balanced_ci95"]
    assert 0.5 <= lo <= 0.75 <= hi <= 1.0
    assert out["mined"]["by_rule"] == {"S1": 1.0}
    assert p2.summarize(scores[:3])["balanced_accuracy"] is None
    assert p2.balanced_ci([True], [True, False]) == (None, None)


# --- one run --------------------------------------------------------------------------------


def calls(*pairs):
    return ChatResponse(
        "m", "", 10, 5, tool_calls=tuple(ToolCallRequest(f"c{i}", n, a) for i, (n, a) in
                                         enumerate(pairs)), output_items=({"type": "x"},)
    )  # fmt: skip


def reply(obj, ticks=None):
    return ChatResponse("m", json.dumps(obj), 20, 7, cost_in_usd_ticks=ticks)


def run(script, t=MINED, **kw):
    client = ScriptedClient(script)
    rec = p2.run_item(t, SOURCES, client, MODEL, p2.Condition.BASELINE, run_id="r1",
                      timer=lambda: 0.0, **kw)  # fmt: skip
    return rec, client


def test_run_item_reads_both_sources_and_scores():
    rec, client = run([
        calls(("get_sdr", {"control_number": BASE.control_number})),
        calls(("get_registration", {"n_number": "101"})),
        reply({"status": "refused", "answer": None, "flags": [p2.FLAG]}, ticks=10**8),
    ])  # fmt: skip
    assert rec.score["correct"] and rec.tool_calls == ("get_sdr", "get_registration")
    assert rec.turns == 3 and rec.input_tokens == 40 and rec.cost_usd is None  # unpriced turns
    assert rec.reference["correct"] and rec.exclusion is None
    tool_msgs = [m for m in client.requests[-1].messages if m["role"] == "tool"]
    assert json.loads(tool_msgs[1]["content"])["registration"]["serial_number"] == "ASN-1001"
    assert client.requests[1].messages[1]["output_items"] == [{"type": "x"}]


def test_run_item_costs_from_ticks_or_price():
    rec, _ = run([reply({"status": "ok", "answer": "ASN-1001"}, ticks=10**8)], t=CONTROL)
    assert rec.cost_usd == pytest.approx(0.01) and rec.score["correct"]
    price = Pricing(1.0, 2.0, "test")
    rec, _ = run([reply({"status": "ok", "answer": "ASN-1001"})], t=CONTROL, pricing=price)
    assert rec.cost_usd == pytest.approx(price.cost(20, 7))


def test_no_verdict_turn_cap_and_errors():
    rec, _ = run([ChatResponse("m", "I think it is fine", 1, 1)])
    assert rec.score["no_verdict"] and not rec.score["correct"] and rec.exclusion is None
    rec, _ = run([calls(("get_sdr", {"control_number": "x"}))] * p2.MAX_TURNS)
    assert rec.turn_cap_hit and rec.score["no_verdict"]
    rec, _ = run([XaiError("boom")])
    assert rec.exclusion == "RUN_ERROR" and rec.score is None and "boom" in rec.error
    assert p2.scores_of([rec]) == []
    q = QuotaExhausted("spent")
    with pytest.raises(QuotaExhausted) as got:
        run([q], t=CONTROL)
    assert got.value.paid_usd == 0


# --- offline demo ---------------------------------------------------------------------------


def test_synthetic_sources_hold_every_settled_rule():
    sdrs, reg = p2.synthetic_sources(200, seed=3)
    counts = mining.mine(sdrs, reg).counts()
    assert counts["S1"] and counts["T1"] and counts["D1"] and counts["controls"]
    assert counts["skip:no_n_number"] and counts["M1"] == 0
    assert sdrs[0].difficulty_date >= date(2026, 1, 1)


def test_demo_shows_blanket_flagging_and_ignoring_the_registry_both_score_half(capsys):
    out = p2.demo(150, seed=5)
    pol = out["policies"]
    assert pol["oracle"]["balanced_accuracy"] == 1.0 == out["reference"]["balanced_accuracy"]
    assert pol["blanket_flagger"]["balanced_accuracy"] == 0.5
    assert pol["blanket_flagger"]["mined"]["correct"] == 1.0
    assert pol["sdr_only"]["balanced_accuracy"] == 0.5
    assert pol["sdr_only"]["mined"]["answered_from_sdr"] == 1.0
    with pytest.raises(p2.Phase2Error, match="policy"):
        p2._scripted_reply("random", MINED)
    assert p2.main(["--rows", "40", "--seed", "2"]) == 0
    assert '"oracle"' in capsys.readouterr().out


def test_plan_items_takes_every_rare_item_and_matches_the_field_mix():
    def it(rule, n, field, expected="disagree"):
        return mining.Item(rule, f"C{n}", "101", field, "a", "b", expected)

    mined = mining.Mined(
        settled=[it("S1", n, "aircraft_serial_number") for n in range(50)]
        + [it("T1", 100 + n, "aircraft_total_time") for n in range(3)]
        + [it("D1", 200, "difficulty_date")],
        unsettled=[it("S2", 300, "aircraft_serial_number")],
        controls=[it("control", 400 + n, "aircraft_serial_number", "agree") for n in range(40)]
        + [it("control", 500 + n, "aircraft_total_time", "agree") for n in range(40)],
        skips=[],
    )
    settled, controls = p2.plan_items(mined, 10, 10, salt="s")
    assert len(settled) == 10 and {i.rule for i in settled[:4]} == {"T1", "D1"}
    assert sum(i.rule == "S1" for i in settled) == 6 and all(i.rule != "S2" for i in settled)
    assert len(controls) == 10
    assert sum(c.field == "aircraft_total_time" for c in controls) == 4  # T1 + D1 share
    assert p2.plan_items(mined, 10, 10, salt="s") == (settled, controls)
    empty = mining.Mined([], [], mined.controls, [])
    assert p2.plan_items(empty, 10, 10, salt="s") == ([], [])
