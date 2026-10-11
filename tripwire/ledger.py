"""Tamper-evident injection ledger (M3): append-only, sha256 hash-chained JSONL.

Each fault the middleware plants is one :class:`LedgerEvent` (run id, tool, fault, field,
original value, served value, timestamp), chained to the previous event by sha256 over the
canonical JSON of the event without its own ``hash`` (the same chain construction as
``tripwire.corrupt``, whose ``ZERO_HASH`` genesis is reused).

A chain alone cannot show that its last entries were deleted, so the latest head
(``count`` and ``hash``) is also stored in a sidecar file ``<ledger>.head`` and returned by
:attr:`Ledger.head`. :func:`verify_ledger` fails on an edited event, a reordered pair, a
removed interior event, and a removed tail event. A forger who rewrites every hash *and* the
sidecar defeats it; keep the returned head somewhere the writer of the ledger cannot reach.

Fails closed: a missing or malformed sidecar, an unparseable line, or a non-finite number is a
:class:`LedgerError` (or ``False`` from :func:`verify_file`), never a pass. A ledger file is
never reopened for append: a new run needs a new path.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from tripwire.corrupt import ZERO_HASH

_FIELDS = (
    "seq",
    "run_id",
    "tool",
    "fault",
    "field",
    "original",
    "served",
    "timestamp",
    "prev_hash",
    "hash",
)


class LedgerError(ValueError):
    """The ledger cannot be written, read, or trusted."""


@dataclass(frozen=True)
class LedgerEvent:
    """One planted fault, chained into the ledger."""

    seq: int  # 1-based position in the chain
    run_id: str
    tool: str
    fault: str  # "F1".."F6"
    field: str  # dotted path of the changed field, e.g. "spec.maximum.value"
    original: Any  # JSON value the clean server would have served
    served: Any  # JSON value actually served (null: the field was omitted)
    timestamp: str  # ISO-8601, tz-aware
    prev_hash: str
    hash: str


@dataclass(frozen=True)
class LedgerHead:
    """The tail-hash protection value: how many events, and the last event's hash."""

    count: int
    hash: str

    @classmethod
    def genesis(cls) -> LedgerHead:
        return cls(0, ZERO_HASH)


def _canonical(payload: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise LedgerError(f"event is not canonical JSON: {exc}") from exc


def _payload(event: LedgerEvent, *, include_hash: bool) -> dict[str, Any]:
    out = {name: getattr(event, name) for name in _FIELDS}
    if not include_hash:
        del out["hash"]
    return out


def compute_hash(event: LedgerEvent) -> str:
    """sha256 over the canonical JSON of ``event`` without its own hash."""
    return hashlib.sha256(_canonical(_payload(event, include_hash=False)).encode()).hexdigest()


def verify_ledger(events: Sequence[LedgerEvent], head: LedgerHead) -> bool:
    """True only if the chain is intact *and* ends exactly at ``head`` (catches a tail delete)."""
    prev_hash = ZERO_HASH
    try:
        for i, event in enumerate(events, start=1):
            if event.seq != i or event.prev_hash != prev_hash or compute_hash(event) != event.hash:
                return False
            prev_hash = event.hash
    except LedgerError:
        return False
    return head.count == len(events) and head.hash == prev_hash


def _event_from_json(raw: object) -> LedgerEvent:
    if not isinstance(raw, dict) or set(raw) != set(_FIELDS):
        raise LedgerError("ledger line does not have exactly the event fields")
    return LedgerEvent(**raw)


def _head_path(path: Path) -> Path:
    return path.with_name(path.name + ".head")


def read_ledger(path: str | Path) -> tuple[list[LedgerEvent], LedgerHead]:
    """Load a ledger file and its sidecar head; anything missing or malformed raises."""
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        events = [_event_from_json(json.loads(line)) for line in lines]
        raw = json.loads(_head_path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # includes JSONDecodeError and LedgerError
        raise LedgerError(f"cannot read ledger {path}: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"count", "hash"}:
        raise LedgerError("ledger head sidecar must hold exactly count and hash")
    return events, LedgerHead(raw["count"], raw["hash"])


def verify_file(path: str | Path) -> bool:
    """:func:`verify_ledger` on a ledger file; any read failure is a failed verification."""
    try:
        events, head = read_ledger(path)
    except LedgerError:
        return False
    return verify_ledger(events, head)


class Ledger:
    """Append-only ledger, in memory and (when ``path`` is given) mirrored to JSONL + sidecar."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._events: list[LedgerEvent] = []
        self._head = LedgerHead.genesis()
        self._path = Path(path) if path is not None else None
        if self._path is not None:
            if self._path.exists() or _head_path(self._path).exists():
                raise LedgerError(f"{self._path} already exists; a ledger is never reopened")
            self._path.touch()
            self._write_head()

    @property
    def events(self) -> tuple[LedgerEvent, ...]:
        return tuple(self._events)

    @property
    def head(self) -> LedgerHead:
        """The latest head: also the value to keep outside the ledger's own storage."""
        return self._head

    def append(
        self,
        *,
        run_id: str,
        tool: str,
        fault: str,
        field: str,
        original: Any,
        served: Any,
        timestamp: datetime,
    ) -> LedgerEvent:
        """Chain one event to the tail and persist it; returns the finished event."""
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise LedgerError("timestamp must be timezone-aware")
        draft = LedgerEvent(
            seq=len(self._events) + 1,
            run_id=run_id,
            tool=tool,
            fault=fault,
            field=field,
            original=original,
            served=served,
            timestamp=timestamp.isoformat(),
            prev_hash=self._head.hash,
            hash="",
        )
        event = replace(draft, hash=compute_hash(draft))
        line = _canonical(_payload(event, include_hash=True))
        if self._path is not None:
            with self._path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")
        self._events.append(event)
        self._head = LedgerHead(event.seq, event.hash)
        if self._path is not None:
            self._write_head()
        return event

    def _write_head(self) -> None:
        assert self._path is not None
        side = _head_path(self._path)
        tmp = side.with_name(side.name + ".tmp")
        tmp.write_text(_canonical({"count": self._head.count, "hash": self._head.hash}), "utf-8")
        os.replace(tmp, side)  # atomic: the sidecar is never half-written
