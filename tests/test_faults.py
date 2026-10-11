"""Fault middleware: one test per fault, determinism, clean mode. In-process, no network."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from tripwire import contract as c
from tripwire import faults as f
from tripwire import server as srv
from tripwire import standards as s
from tripwire import synthdata as sd
from tripwire.faults import FaultConfig, FaultKind, FaultSpec
from tripwire.ledger import Ledger, verify_ledger
from tripwire.store import NotFound, Store

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
DS = sd.generate(1)
BRACKET = {
    "spec_id": "TW-SRM-PN-10001",
    "parameter": "crack_length_max",
    "applies_to": "BRACKET PN-10001",
}
CRIMP = {"spec_id": "NASA-STD-8739.4A", "parameter": "crimp_tensile_min", "applies_to": "AWG 22"}
WO = {"defect_id": DS.defects[0].defect_id, "action": "replace part", "created_by": "agent-01"}
TEST_ID = {"test_id": DS.test_results[0].test_id}
WO_ID = {"work_order_id": DS.work_orders[0].work_order_id}


def _injector(kinds, seed=0, run_id="run-a"):
    cfg = FaultConfig(seed, tuple(FaultSpec(FaultKind(k)) for k in kinds))
    store = Store.from_dataset(DS)
    return f.FaultInjector(cfg, Ledger(), run_id, store, lambda: NOW), store


def _session(store, inj, calls):
    """Run ``calls`` [(tool, args)] on one server; returns the CallToolResults."""
    mcp = srv.build_mcp(store, lambda: NOW, inj)

    async def run():
        async with create_connected_server_and_client_session(mcp) as session:
            return [await session.call_tool(t, a) for t, a in calls]

    return asyncio.run(run())


def _flat(node, path=""):
    """{dotted path: leaf} for a JSON value; a list's length is its own entry."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            out.update(_flat(v, f"{path}.{k}" if path else k))
        return out
    if isinstance(node, list):
        out = {f"{path}#len": len(node)}
        for i, v in enumerate(node):
            out.update(_flat(v, f"{path}[{i}]"))
        return out
    return {path: node}


def _changed(clean, served):
    a, b = _flat(clean), _flat(served)
    return {p for p in a.keys() | b.keys() if a.get(p, "<absent>") != b.get(p, "<absent>")}


def _clean(tool, args):
    (res,) = _session(Store.from_dataset(DS), None, [(tool, args)])
    assert not res.isError
    return res.structuredContent


def _faulted(kinds, tool, args, seed=0):
    inj, store = _injector(kinds, seed)
    (res,) = _session(store, inj, [(tool, args)])
    assert not res.isError, res.content
    return res.structuredContent, inj


def test_f1_unit_swap_changes_only_the_value_and_keeps_the_label():
    clean = _clean("get_spec", BRACKET)
    served, inj = _faulted(["F1"], "get_spec", BRACKET)
    orig = clean["spec"]["maximum"]["value"]
    want = s.convert(orig, s.Unit.INCH, s.Unit.MILLIMETRE)
    assert served["spec"]["maximum"] == {"value": want, "unit": "in"}  # label left unchanged
    assert _changed(clean, served) == {"spec.maximum.value"}
    (ev,) = inj.ledger.events
    assert (ev.tool, ev.fault, ev.field) == ("get_spec", "F1", "spec.maximum.value")
    assert (ev.original, ev.served, ev.run_id) == (orig, want, "run-a")


def test_f1_force_uses_the_exact_standards_factor():
    clean = _clean("get_spec", CRIMP)
    served, inj = _faulted(["F1"], "get_spec", CRIMP)
    want = s.convert(clean["spec"]["minimum"]["value"], s.Unit.NEWTON, s.Unit.POUND_FORCE)
    assert served["spec"]["minimum"] == {"value": want, "unit": "N"}
    assert _changed(clean, served) == {"spec.minimum.value"}
    assert inj.ledger.events[0].field == "spec.minimum.value"


def test_f1_on_a_defect_list_touches_one_quantity():
    clean = _clean("query_defects", {"limit": 3})
    served, inj = _faulted(["F1"], "query_defects", {"limit": 3})
    (ev,) = inj.ledger.events
    assert _changed(clean, served) == {ev.field} and ev.field.endswith(".value")
    assert ev.field.startswith("defects[0].") and ev.served != ev.original


def test_f2_stale_revision_is_served_as_current():
    clean = _clean("get_spec", BRACKET)
    served, inj = _faulted(["F2"], "get_spec", BRACKET)
    old = c.to_jsonable(Store.from_dataset(DS).get_spec(*BRACKET.values(), revision="r1"))
    assert clean["spec"]["revision"] == "r2" and served["spec"]["revision"] == "r1"
    assert served["spec"]["superseded_by"] is None  # labelled current
    assert served["spec"] == {**old, "superseded_by": None}
    (ev,) = inj.ledger.events
    assert (ev.fault, ev.field) == ("F2", "spec")
    assert (ev.original, ev.served) == (clean["spec"], served["spec"])


def test_f2_does_not_fire_on_pinned_revisions_or_single_revision_specs():
    inj, store = _injector(["F2"])
    pinned, single = _session(
        store, inj, [("get_spec", {**BRACKET, "revision": "r2"}), ("get_spec", CRIMP)]
    )
    assert pinned.structuredContent["spec"]["revision"] == "r2"
    assert single.structuredContent == _clean("get_spec", CRIMP)
    assert inj.ledger.events == ()


def test_f3_wrong_value_scales_one_value_and_keeps_the_unit():
    clean = _clean("get_test_result", TEST_ID)
    served, inj = _faulted(["F3"], "get_test_result", TEST_ID)
    before, after = clean["test_result"]["measured"], served["test_result"]["measured"]
    assert _changed(clean, served) == {"test_result.measured.value"}
    assert after["unit"] == before["unit"]
    assert min(abs(after["value"] / before["value"] - k) for k in f.WRONG_FACTORS) < 1e-3
    (ev,) = inj.ledger.events
    assert (ev.fault, ev.field) == ("F3", "test_result.measured.value")
    assert (ev.original, ev.served) == (before["value"], after["value"])


def test_f4_silent_noop_write_reports_success_but_the_store_has_nothing():
    inj, store = _injector(["F4"])
    before = store.snapshot()
    (create,) = _session(store, inj, [("create_work_order", WO)])
    assert not create.isError
    wo = create.structuredContent["work_order"]
    assert wo["work_order_id"] == "WO-900001" and wo["status"] == "open"
    assert (wo["defect_id"], wo["action"]) == (WO["defect_id"], WO["action"])
    with pytest.raises(NotFound):  # the real store
        store.get_work_order(wo["work_order_id"])
    assert store.snapshot() == before
    (ev,) = inj.ledger.events
    assert (ev.tool, ev.fault, ev.field) == ("create_work_order", "F4", "work_order.work_order_id")
    assert (ev.original, ev.served) == (None, "WO-900001")


def test_f4_readback_through_the_server_is_not_found_and_ids_are_distinct():
    inj, store = _injector(["F4"])
    a, b = _session(store, inj, [("create_work_order", WO), ("create_work_order", WO)])
    ids = [r.structuredContent["work_order"]["work_order_id"] for r in (a, b)]
    assert ids == ["WO-900001", "WO-900002"]
    (read,) = _session(store, None, [("get_work_order", {"work_order_id": ids[0]})])
    assert read.isError and "NOT_FOUND" in read.content[0].text


def test_f4_validates_like_the_real_write():
    inj, store = _injector(["F4"])
    missing, blank = _session(
        store,
        inj,
        [
            ("create_work_order", {**WO, "defect_id": "no-such"}),
            ("create_work_order", {**WO, "action": " "}),
        ],
    )
    assert missing.isError and "NOT_FOUND" in missing.content[0].text
    assert blank.isError and "VALIDATION" in blank.content[0].text
    assert inj.ledger.events == ()


def test_f5_truncates_a_list_without_any_marker():
    clean = _clean("query_defects", {"limit": 10})
    served, inj = _faulted(["F5"], "query_defects", {"limit": 10})
    kept = len(served["defects"])
    assert 1 <= kept < len(clean["defects"])
    assert served["defects"] == clean["defects"][:kept]
    assert (served["total"], served["truncated"]) == (clean["total"], clean["truncated"])
    assert _changed(clean, served) == {"defects#len"} | {
        p for p in _flat(clean) if p.startswith(tuple(f"defects[{i}]" for i in range(kept, 10)))
    }
    (ev,) = inj.ledger.events
    assert (ev.fault, ev.field) == ("F5", "defects")
    assert ev.original == [d["defect_id"] for d in clean["defects"]]
    assert ev.served == [d["defect_id"] for d in served["defects"]]


def test_f5_cuts_long_text_when_there_is_no_list():
    clean = _clean("get_work_order", WO_ID)
    served, inj = _faulted(["F5"], "get_work_order", WO_ID)
    full, cut = clean["work_order"]["action"], served["work_order"]["action"]
    assert 1 <= len(cut) < len(full) and full.startswith(cut)
    assert _changed(clean, served) == {"work_order.action"}
    (ev,) = inj.ledger.events
    assert (ev.field, ev.original, ev.served) == ("work_order.action", full, cut)


def test_f5_falls_back_to_text_when_the_list_has_one_item():
    served, inj = _faulted(["F5"], "query_defects", {"limit": 1})
    (ev,) = inj.ledger.events
    assert ev.field == "defects[0].discrepancy" and ev.original.startswith(ev.served)
    assert len(ev.served) < len(ev.original)


def test_f6_drops_one_field_and_records_what_was_omitted():
    clean = _clean("get_spec", BRACKET)
    served, inj = _faulted(["F6"], "get_spec", BRACKET)
    (ev,) = inj.ledger.events
    assert ev.fault == "F6" and ev.served is None
    assert ev.field.rsplit(".", 1)[-1] in f.DROPPABLE
    under = {p for p in _flat(clean) if p == ev.field or p.startswith(ev.field + ".")}
    assert set(_flat(clean)) - set(_flat(served)) == under and under
    assert _changed(clean, served) == under  # nothing else moved
    node = clean
    for part in ev.field.split("."):
        node = node[part]
    assert ev.original == node


def test_clean_mode_is_byte_identical_to_the_m2_server():
    calls = [
        ("query_defects", {"limit": 5}),
        ("get_spec", BRACKET),
        ("get_test_result", TEST_ID),
        ("create_work_order", WO),
        ("get_work_order", WO_ID),
    ]
    baseline = _session(Store.from_dataset(DS), None, calls)
    inj, store = _injector([])  # an injector with no faults configured
    clean = _session(store, inj, calls)

    def dump(results):
        return [json.dumps(r.model_dump(), sort_keys=True) for r in results]

    assert dump(clean) == dump(baseline)
    assert inj.ledger.events == () and inj.head == Ledger().head


def test_same_seed_same_faults_and_ledger_and_a_different_seed_differs():
    calls = [("query_defects", {"limit": 10}), ("get_spec", BRACKET), ("get_spec", CRIMP)] * 2

    def run(seed):
        inj, store = _injector(["F3", "F5", "F6"], seed=seed)
        return [r.structuredContent for r in _session(store, inj, calls)], inj

    out1, a = run(5)
    out2, b = run(5)
    out3, d = run(6)
    assert out1 == out2 and a.ledger.events == b.ledger.events and a.head == b.head
    assert a.ledger.events and out1 != out3 and a.ledger.events != d.ledger.events
    assert verify_ledger(a.ledger.events, a.head)


def _bare(kind, rate, seed):
    cfg = FaultConfig(seed, (FaultSpec(kind, rate),))
    return f.FaultInjector(cfg, Ledger(), "r", Store.from_dataset(DS), lambda: NOW)


def test_rate_zero_never_fires_and_partial_rates_are_seeded():
    q = {"spec": {"maximum": {"value": 2.0, "unit": "in"}}}

    def real():
        return json.loads(json.dumps(q))

    never = _bare(FaultKind.F3, 0.0, 0)
    assert never.intercept("get_spec", {}, real) == q and not never.ledger.events

    def fired(seed):
        inj = _bare(FaultKind.F3, 0.5, seed)
        return [inj.intercept("get_spec", {}, real) != q for _ in range(40)]

    assert fired(1) == fired(1) and 0 < sum(fired(1)) < 40 and fired(1) != fired(2)


def test_first_applicable_fault_wins_and_inapplicable_ones_fall_through():
    _, inj = _faulted(["F4", "F1"], "get_spec", CRIMP)  # F4 never applies to get_spec
    assert [e.fault for e in inj.ledger.events] == ["F1"]
    _, inj = _faulted(["F2", "F3"], "get_spec", CRIMP)  # F2 needs an older revision
    assert [e.fault for e in inj.ledger.events] == ["F3"]


def test_config_validation_and_parse():
    assert f.parse_faults("") == ()
    assert f.parse_faults(" f1 , F6 ") == (FaultSpec(FaultKind.F1), FaultSpec(FaultKind.F6))
    for bad in ("F7", "unit_swap"):
        with pytest.raises(f.FaultConfigError):
            f.parse_faults(bad)
    with pytest.raises(f.FaultConfigError):
        FaultSpec("F1")
    for rate in (-0.1, 1.5, True, "x"):
        with pytest.raises(f.FaultConfigError):
            FaultSpec(FaultKind.F1, rate)
    with pytest.raises(f.FaultConfigError):
        FaultConfig("1")
    with pytest.raises(f.FaultConfigError):
        FaultConfig(1, (FaultSpec(FaultKind.F1), FaultSpec(FaultKind.F1)))
    with pytest.raises(f.FaultConfigError):
        f.FaultInjector(FaultConfig(1), Ledger(), " ", Store.from_dataset(DS), lambda: NOW)


def test_every_fault_has_a_name_and_tools():
    assert set(f.FAULT_NAMES) == set(f.FAULT_TOOLS) == set(FaultKind)


def test_main_rejects_bad_fault_config(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(srv.TOKEN_ENV, "tok")
    assert srv.main(["--seed", "1", "--faults", "F9"]) == 2
    path = tmp_path / "ledger.jsonl"
    path.write_text("")
    assert srv.main(["--seed", "1", "--faults", "F1", "--ledger", str(path)]) == 2
    assert "error:" in capsys.readouterr().err
