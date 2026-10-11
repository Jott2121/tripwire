"""Contract tests for the MCP server: in-process only (SDK memory transport + ASGI client)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from tripwire import contract as c
from tripwire import server as srv
from tripwire import synthdata as sd
from tripwire.store import Store

TOKEN = "s3cr3t-token-value"
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
DS = sd.generate(1)
BRACKET = ("TW-SRM-PN-10001", "crack_length_max", "BRACKET PN-10001")


def _store() -> Store:
    return Store.from_dataset(DS)


def _canon(payload: dict) -> str:
    """The same canonical form as ``contract.canonical_json``, for an already-plain payload."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(_canon(payload).encode("utf-8")).hexdigest()


def _call(store: Store, tool: str, args: dict):
    """One tool call over the in-memory transport; returns the CallToolResult."""
    mcp = srv.build_mcp(store, clock=lambda: NOW)

    async def run():
        async with create_connected_server_and_client_session(mcp) as session:
            return await session.call_tool(tool, args)

    return asyncio.run(run())


def _ok(store: Store, tool: str, args: dict) -> dict:
    res = _call(store, tool, args)
    assert not res.isError, res.content
    return res.structuredContent


def _err(store: Store, tool: str, args: dict, prefix: str) -> str:
    res = _call(store, tool, args)
    assert res.isError
    text = res.content[0].text
    assert prefix in text
    return text


def test_exactly_the_five_tools_are_listed():
    mcp = srv.build_mcp(_store())

    async def run():
        async with create_connected_server_and_client_session(mcp) as session:
            return await session.list_tools()

    assert tuple(t.name for t in asyncio.run(run()).tools) == srv.TOOLS


def test_query_defects_payload_carries_units_and_provenance():
    out = _ok(_store(), "query_defects", {"jasc_code": DS.defects[0].jasc_code})
    want = _store().query_defects(jasc_code=DS.defects[0].jasc_code)
    assert out == c.to_jsonable(want)
    first = out["defects"][0]
    assert first["provenance"]["source"] and first["provenance"]["retrieved_at"]
    assert first["aircraft_total_time"]["unit"] == "h"


def test_query_defects_truncation_is_explicit():
    out = _ok(_store(), "query_defects", {"limit": 3})
    assert len(out["defects"]) == 3 and out["total"] == len(DS.defects) and out["truncated"]
    out = _ok(_store(), "query_defects", {"limit": 1000})
    assert out["total"] == len(out["defects"]) and not out["truncated"]


@pytest.mark.parametrize(
    "args",
    [
        {"limit": 0},
        {"limit": -1},
        {"limit": "ten"},
        {"since": "not-a-date"},
        {"since": "2026-01-01T00:00:00"},
        {"nonsense": 1},
    ],
)
def test_query_defects_bad_arguments_are_errors(args):
    res = _call(_store(), "query_defects", args)
    assert res.isError


def test_get_spec_current_exact_revision_and_misses():
    st = _store()
    spec_id, parameter, applies_to = BRACKET
    cur = _ok(st, "get_spec", dict(spec_id=spec_id, parameter=parameter, applies_to=applies_to))
    assert cur["spec"] == c.to_jsonable(st.get_spec(*BRACKET))
    assert cur["spec"]["revision"] == "r2" and cur["spec"]["maximum"]["unit"] == "in"
    old = _ok(
        st,
        "get_spec",
        dict(spec_id=spec_id, parameter=parameter, applies_to=applies_to, revision="r1"),
    )
    assert old["spec"]["revision"] == "r1" and old["spec"]["superseded_by"] == "r2"
    miss = dict(spec_id=spec_id, parameter=parameter, applies_to="NOPE")
    assert "NOPE" in _err(st, "get_spec", miss, "NOT_FOUND")
    _err(st, "get_spec", {**miss, "applies_to": applies_to, "revision": "r9"}, "NOT_FOUND")
    assert _call(st, "get_spec", {"spec_id": spec_id}).isError  # missing arguments


def test_get_test_result_round_trips_and_misses():
    st = _store()
    t = DS.test_results[0]
    out = _ok(st, "get_test_result", {"test_id": t.test_id})
    assert out["test_result"] == c.to_jsonable(t)
    assert out["test_result"]["applies_to"] == t.applies_to
    assert out["test_result"]["measured"]["unit"] in ("in", "mm")
    assert "TR-nope" in _err(st, "get_test_result", {"test_id": "TR-nope"}, "NOT_FOUND")


def test_create_then_get_work_order_is_byte_identical_and_snapshot_changes_by_one_key():
    st = _store()
    before = st.snapshot()
    args = {"defect_id": DS.defects[0].defect_id, "action": "replace part", "created_by": "tester"}
    created = _ok(st, "create_work_order", args)["work_order"]
    assert created["created_at"] == NOW.isoformat() and created["status"] == "open"
    again = _ok(st, "get_work_order", {"work_order_id": created["work_order_id"]})["work_order"]
    assert again == created
    stored = c.fingerprint(st.get_work_order(created["work_order_id"]))
    assert _fingerprint(created) == _fingerprint(again) == stored
    after = st.snapshot()
    assert set(after) - set(before) == {created["work_order_id"]}
    assert {k: v for k, v in after.items() if k in before} == before


def test_failed_create_changes_nothing():
    st = _store()
    before = st.snapshot()
    ok = {"defect_id": DS.defects[0].defect_id, "action": "x", "created_by": "t"}
    _err(st, "create_work_order", {**ok, "defect_id": "NO-SUCH"}, "NOT_FOUND")
    _err(st, "create_work_order", {**ok, "action": " "}, "VALIDATION")
    assert _call(st, "create_work_order", {**ok, "created_by": 5}).isError
    assert _call(st, "create_work_order", {**ok, "extra": 1}).isError
    assert st.snapshot() == before


def test_get_work_order_seeded_and_miss():
    st = _store()
    wo = DS.work_orders[0]
    assert _ok(st, "get_work_order", {"work_order_id": wo.work_order_id})["work_order"] == (
        c.to_jsonable(wo)
    )
    assert "WO-x" in _err(st, "get_work_order", {"work_order_id": "WO-x"}, "NOT_FOUND")


def test_same_seed_gives_identical_canonical_outputs():
    calls = [
        ("query_defects", {"limit": 7}),
        ("get_spec", dict(zip(("spec_id", "parameter", "applies_to"), BRACKET, strict=True))),
        ("get_test_result", {"test_id": DS.test_results[0].test_id}),
    ]
    for tool, args in calls:
        a = _canon(_ok(Store.from_dataset(sd.generate(1)), tool, args))
        b = _canon(_ok(Store.from_dataset(sd.generate(1)), tool, args))
        assert a == b


# --- auth -----------------------------------------------------------------------------------

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def _post(headers: dict[str, str], token: str | None = TOKEN):
    app, mcp = srv.build_app(_store(), token)

    async def run():
        async with mcp.session_manager.run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8000"
            ) as cl:
                return await cl.post("/mcp", json=INIT, headers={**HEADERS, **headers})

    return asyncio.run(run())


def test_correct_token_is_allowed():
    res = _post({"authorization": f"Bearer {TOKEN}"})
    assert res.status_code == 200 and res.json()["result"]["serverInfo"]["name"] == "tripwire"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": "Bearer wrong"},
        {"authorization": "Bearer "},
        {"authorization": TOKEN},
        {"authorization": f"Basic {TOKEN}"},
        {"authorization": f"Bearer {TOKEN}x"},
    ],
)
def test_missing_or_wrong_token_is_denied_without_leaking_it(headers, caplog):
    res = _post(headers)
    assert res.status_code == 401
    assert TOKEN not in res.text and TOKEN not in caplog.text
    assert "wrong" not in res.text


def test_server_without_a_token_refuses_to_start(monkeypatch, capsys):
    for bad in (None, ""):
        with pytest.raises(srv.ServerConfigError) as exc:
            srv.build_app(_store(), bad)
        assert TOKEN not in str(exc.value)
    monkeypatch.delenv(srv.TOKEN_ENV, raising=False)
    assert srv.main(["--seed", "1"]) == 2
    assert srv.TOKEN_ENV in capsys.readouterr().err


def test_non_http_scopes_are_denied():
    sent = []

    async def inner(scope, receive, send):  # pragma: no cover - must not be reached
        raise AssertionError("inner app reached")

    async def send(msg):
        sent.append(msg)

    asyncio.run(srv.BearerAuth(inner, TOKEN)({"type": "websocket", "headers": []}, None, send))
    assert sent == []
