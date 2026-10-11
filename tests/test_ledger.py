"""Ledger: chain, tail-hash sidecar, and the four tamper types."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from tripwire import ledger as lg
from tripwire.ledger import Ledger, LedgerError, LedgerHead, verify_file, verify_ledger

T = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def _fill(ledger: Ledger, n: int = 4) -> Ledger:
    for i in range(n):
        ledger.append(
            run_id="r",
            tool="get_spec",
            fault="F1",
            field=f"spec.maximum.value{i}",
            original=1.0 + i,
            served=25.4 * (1.0 + i),
            timestamp=T,
        )
    return ledger


def _rehash(events):
    """What a forger does after editing: recompute every hash so the chain alone looks fine."""
    out, prev = [], lg.ZERO_HASH
    for e in events:
        e = replace(e, prev_hash=prev)
        e = replace(e, hash=lg.compute_hash(e))
        out.append(e)
        prev = e.hash
    return out


def test_chain_links_and_head_tracks_the_tail():
    led = _fill(Ledger())
    evs = led.events
    assert [e.seq for e in evs] == [1, 2, 3, 4]
    assert evs[0].prev_hash == lg.ZERO_HASH and evs[1].prev_hash == evs[0].hash
    assert led.head == LedgerHead(4, evs[-1].hash)
    assert verify_ledger(evs, led.head)
    assert Ledger().head == LedgerHead.genesis() and verify_ledger((), LedgerHead.genesis())


def test_event_records_every_required_field():
    (e,) = _fill(Ledger(), 1).events
    assert (e.run_id, e.tool, e.fault, e.field) == ("r", "get_spec", "F1", "spec.maximum.value0")
    assert (e.original, e.served, e.timestamp) == (1.0, 25.4, T.isoformat())


def test_edit_is_detected():
    led = _fill(Ledger())
    evs = list(led.events)
    evs[1] = replace(evs[1], served=999.0)
    assert not verify_ledger(evs, led.head)
    assert not verify_ledger(_rehash(evs), led.head)  # even re-hashed: the head moved


def test_reorder_is_detected():
    led = _fill(Ledger())
    evs = list(led.events)
    evs[1], evs[2] = evs[2], evs[1]
    assert not verify_ledger(evs, led.head)
    assert not verify_ledger(_rehash(evs), led.head)


def test_interior_delete_is_detected():
    led = _fill(Ledger())
    evs = list(led.events)
    del evs[1]
    assert not verify_ledger(evs, led.head)
    assert not verify_ledger(_rehash(evs), led.head)


def test_tail_delete_is_detected_only_because_of_the_head():
    led = _fill(Ledger())
    evs = list(led.events)[:-1]
    assert not verify_ledger(evs, led.head)
    # the bare chain of the truncated ledger is internally consistent: the head is what catches it
    assert verify_ledger(evs, LedgerHead(len(evs), evs[-1].hash))
    assert not verify_ledger((), led.head)  # delete everything


def test_non_finite_numbers_and_naive_timestamps_are_refused(tmp_path):
    led = Ledger()
    with pytest.raises(LedgerError, match="canonical JSON"):
        led.append(
            run_id="r",
            tool="t",
            fault="F3",
            field="f",
            original=float("nan"),
            served=1,
            timestamp=T,
        )
    with pytest.raises(LedgerError, match="timezone-aware"):
        led.append(
            run_id="r",
            tool="t",
            fault="F3",
            field="f",
            original=1,
            served=2,
            timestamp=T.replace(tzinfo=None),
        )
    assert led.events == () and led.head == LedgerHead.genesis()


def test_a_non_finite_event_fails_verification_instead_of_crashing():
    led = _fill(Ledger(), 2)
    bad = replace(led.events[0], served=float("inf"))
    assert not verify_ledger([bad, led.events[1]], led.head)


def test_file_round_trip_and_sidecar(tmp_path):
    path = tmp_path / "run.jsonl"
    led = _fill(Ledger(path))
    side = tmp_path / "run.jsonl.head"
    assert json.loads(side.read_text()) == {"count": 4, "hash": led.head.hash}
    assert len(path.read_text().splitlines()) == 4
    events, head = lg.read_ledger(path)
    assert events == list(led.events) and head == led.head
    assert verify_file(path)
    assert not (tmp_path / "run.jsonl.head.tmp").exists()


def test_file_tail_delete_is_detected(tmp_path):
    path = tmp_path / "run.jsonl"
    _fill(Ledger(path))
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n")  # the sidecar still names the old head
    assert not verify_file(path)


def test_file_edit_reorder_and_interior_delete_are_detected(tmp_path):
    path = tmp_path / "run.jsonl"
    _fill(Ledger(path))
    lines = path.read_text().splitlines()
    cases = {
        "edit": [lines[0], lines[1].replace("50.8", "51.8"), *lines[2:]],
        "reorder": [lines[0], lines[2], lines[1], lines[3]],
        "interior": [lines[0], lines[2], lines[3]],
    }
    for name, new in cases.items():
        path.write_text("\n".join(new) + "\n")
        assert not verify_file(path), name
    path.write_text("\n".join(lines) + "\n")
    assert verify_file(path)


def test_a_forger_who_rewrites_chain_and_sidecar_is_not_caught_by_the_file_alone(tmp_path):
    """Documented limit: keep the returned head outside the ledger's storage."""
    path = tmp_path / "run.jsonl"
    led = _fill(Ledger(path))
    forged = _rehash(list(led.events)[:-1])
    path.write_text(
        "".join(lg._canonical(lg._payload(e, include_hash=True)) + "\n" for e in forged)
    )
    (tmp_path / "run.jsonl.head").write_text(
        json.dumps({"count": len(forged), "hash": forged[-1].hash})
    )
    assert verify_file(path)
    assert not verify_ledger(forged, led.head)  # the head the writer returned still catches it


@pytest.mark.parametrize("damage", ["missing_sidecar", "bad_sidecar", "bad_line", "extra_key"])
def test_unreadable_or_malformed_files_fail_closed(tmp_path, damage):
    path = tmp_path / "run.jsonl"
    _fill(Ledger(path), 2)
    side = tmp_path / "run.jsonl.head"
    if damage == "missing_sidecar":
        side.unlink()
    elif damage == "bad_sidecar":
        side.write_text('{"count": 2}')
    elif damage == "bad_line":
        path.write_text("not json\n")
    else:
        path.write_text(path.read_text().replace('"fault"', '"fault2"'))
    assert not verify_file(path)
    with pytest.raises(LedgerError):
        lg.read_ledger(path)
    assert not verify_file(tmp_path / "absent.jsonl")


def test_a_ledger_file_is_never_reopened_for_append(tmp_path):
    path = tmp_path / "run.jsonl"
    Ledger(path)
    with pytest.raises(LedgerError, match="already exists"):
        Ledger(path)
    other = tmp_path / "other.jsonl"
    (tmp_path / "other.jsonl.head").write_text("{}")
    with pytest.raises(LedgerError, match="already exists"):
        Ledger(other)
