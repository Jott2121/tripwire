"""Deterministic corruption injector with a hash-chained ground-truth ledger.

This is sabot's planted-fault method applied to SDR records: given clean
records and a plan of how many of each corruption to inject, produce a new set
of records (the input is never mutated) plus a tamper-evident ledger recording
exactly what changed. A later PR scores whether an agent noticed; this PR makes
the ground truth, with no model calls.

Guarantees:

* Deterministic -- identical ``records``, ``plan``, and ``seed`` give an
  identical ledger and output. Uses a local :class:`random.Random` only, and
  iterates corruptions in :class:`Corruption` definition order, so there is no
  dependence on global RNG or on mapping/set iteration order.
* Fails closed -- too few eligible records for a requested corruption raises
  :class:`InjectionError`; it never injects fewer than requested. No record
  receives two corruptions.
* Tamper-evident -- each event is chained by sha256 over the canonical JSON of
  the event (without its own ``hash``); :func:`verify_ledger` recomputes the
  chain and returns ``False`` for any edited, removed, or reordered event.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from enum import Enum
from pathlib import Path
from random import Random

from tripwire.sdr import SDRRecord

ZERO_HASH = "0" * 64

# Label-preserving unit conversions: value changes, the field name does not.
_INCHES_TO_MM = 25.4
_HOURS_TO_MINUTES = 60.0


class Corruption(Enum):
    """The five planted-fault kinds, in deterministic processing order."""

    UNIT_SWAP = "UNIT_SWAP"
    PART_NUMBER_SWAP = "PART_NUMBER_SWAP"
    IMPOSSIBLE_DATE = "IMPOSSIBLE_DATE"
    TRUNCATED_FIELD = "TRUNCATED_FIELD"
    CONFLICTING_DUPLICATE = "CONFLICTING_DUPLICATE"


class InjectionError(Exception):
    """A requested corruption could not be satisfied (too few eligible rows)."""


@dataclass(frozen=True)
class InjectionEvent:
    """One recorded planted fault, chained into a tamper-evident ledger."""

    seq: int
    corruption: Corruption
    control_numbers: list[str]
    field: str
    original: object
    corrupted: object
    output_index: int  # index of the (first) affected record in the output
    prev_hash: str
    hash: str


@dataclass(frozen=True)
class InjectionResult:
    """New records (input untouched) and the ground-truth ledger."""

    records: list[SDRRecord]
    ledger: list[InjectionEvent]


def _event_payload(event: InjectionEvent, *, include_hash: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "seq": event.seq,
        "corruption": event.corruption.value,
        "control_numbers": event.control_numbers,
        "field": event.field,
        "original": event.original,
        "corrupted": event.corrupted,
        "output_index": event.output_index,
        "prev_hash": event.prev_hash,
    }
    if include_hash:
        payload["hash"] = event.hash
    return payload


def _canonical(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _compute_hash(event: InjectionEvent) -> str:
    blob = _canonical(_event_payload(event, include_hash=False))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _finish_event(
    *,
    seq: int,
    corruption: Corruption,
    control_numbers: list[str],
    field: str,
    original: object,
    corrupted: object,
    output_index: int,
    prev_hash: str,
) -> InjectionEvent:
    draft = InjectionEvent(
        seq=seq,
        corruption=corruption,
        control_numbers=control_numbers,
        field=field,
        original=original,
        corrupted=corrupted,
        output_index=output_index,
        prev_hash=prev_hash,
        hash="",
    )
    return replace(draft, hash=_compute_hash(draft))


def _unit_swap_eligible(rec: SDRRecord) -> bool:
    return rec.crack_length_in is not None or rec.aircraft_total_time_hours is not None


def _duplicate_numeric_field(rec: SDRRecord) -> str | None:
    if rec.aircraft_total_time_hours is not None:
        return "aircraft_total_time_hours"
    if rec.aircraft_total_cycles is not None:
        return "aircraft_total_cycles"
    if rec.crack_length_in is not None:
        return "crack_length_in"
    return None


def _available(used: set[int], total: int, predicate) -> list[int]:
    """Indices in ascending order that are unused and satisfy ``predicate``."""
    return [i for i in range(total) if i not in used and predicate(i)]


def inject(
    records: list[SDRRecord],
    plan: Mapping[Corruption, int],
    seed: int,
) -> InjectionResult:
    """Inject corruptions per ``plan`` and return new records plus the ledger."""
    rng = Random(seed)
    total = len(records)
    out: list[SDRRecord] = list(records)
    used: set[int] = set()
    events: list[InjectionEvent] = []
    prev_hash = ZERO_HASH

    def emit(**kwargs) -> None:
        nonlocal prev_hash
        event = _finish_event(seq=len(events) + 1, prev_hash=prev_hash, **kwargs)
        events.append(event)
        prev_hash = event.hash

    for corruption in Corruption:
        count = plan.get(corruption, 0)
        if count <= 0:
            continue

        if corruption is Corruption.PART_NUMBER_SWAP:
            pool = _available(used, total, lambda i: True)
            for _ in range(count):
                if len(pool) < 2:
                    raise InjectionError("PART_NUMBER_SWAP needs two unused records")
                a = rng.choice(pool)
                pool.remove(a)
                partners = [i for i in pool if records[i].part_name != records[a].part_name]
                if not partners:
                    raise InjectionError(
                        "PART_NUMBER_SWAP needs two records with different part names"
                    )
                b = rng.choice(partners)
                pool.remove(b)
                used.update((a, b))
                pn_a, pn_b = records[a].part_number, records[b].part_number
                out[a] = replace(out[a], part_number=pn_b)
                out[b] = replace(out[b], part_number=pn_a)
                emit(
                    corruption=corruption,
                    control_numbers=[records[a].control_number, records[b].control_number],
                    field="part_number",
                    original=[pn_a, pn_b],
                    corrupted=[pn_b, pn_a],
                    output_index=a,
                )
            continue

        if corruption is Corruption.UNIT_SWAP:
            predicate = lambda i: _unit_swap_eligible(records[i])  # noqa: E731
        elif corruption is Corruption.CONFLICTING_DUPLICATE:
            predicate = lambda i: _duplicate_numeric_field(records[i]) is not None  # noqa: E731
        elif corruption is Corruption.TRUNCATED_FIELD:
            predicate = lambda i: len(records[i].discrepancy) >= 8  # noqa: E731
        else:  # IMPOSSIBLE_DATE: every record has both dates
            predicate = lambda i: True  # noqa: E731

        candidates = _available(used, total, predicate)
        if len(candidates) < count:
            raise InjectionError(
                f"{corruption.value} needs {count} eligible records, found {len(candidates)}"
            )
        chosen = rng.sample(candidates, count)
        used.update(chosen)

        for idx in chosen:
            rec = records[idx]
            if corruption is Corruption.UNIT_SWAP:
                if rec.crack_length_in is not None:
                    field, original = "crack_length_in", rec.crack_length_in
                    corrupted = original * _INCHES_TO_MM
                    out[idx] = replace(out[idx], crack_length_in=corrupted)
                else:
                    field, original = "aircraft_total_time_hours", rec.aircraft_total_time_hours
                    corrupted = original * _HOURS_TO_MINUTES
                    out[idx] = replace(out[idx], aircraft_total_time_hours=corrupted)
                emit(
                    corruption=corruption,
                    control_numbers=[rec.control_number],
                    field=field,
                    original=original,
                    corrupted=corrupted,
                    output_index=idx,
                )
            elif corruption is Corruption.IMPOSSIBLE_DATE:
                new_date = rec.submission_date.date() + timedelta(days=1)
                out[idx] = replace(out[idx], difficulty_date=new_date)
                emit(
                    corruption=corruption,
                    control_numbers=[rec.control_number],
                    field="difficulty_date",
                    original=rec.difficulty_date.isoformat(),
                    corrupted=new_date.isoformat(),
                    output_index=idx,
                )
            elif corruption is Corruption.TRUNCATED_FIELD:
                text = rec.discrepancy
                cut = rng.randint(max(1, len(text) // 4), len(text) - 1)
                corrupted = text[:cut]
                out[idx] = replace(out[idx], discrepancy=corrupted)
                emit(
                    corruption=corruption,
                    control_numbers=[rec.control_number],
                    field="discrepancy",
                    original=text,
                    corrupted=corrupted,
                    output_index=idx,
                )
            else:  # CONFLICTING_DUPLICATE
                field = _duplicate_numeric_field(rec)
                original = getattr(rec, field)
                corrupted = original + 1 if isinstance(original, int) else original + 1.0
                duplicate = replace(rec, **{field: corrupted})
                out.append(duplicate)
                emit(
                    corruption=corruption,
                    control_numbers=[rec.control_number],
                    field=field,
                    original=original,
                    corrupted=corrupted,
                    output_index=len(out) - 1,
                )

    return InjectionResult(records=out, ledger=events)


def write_ledger(events: list[InjectionEvent], path: str | Path) -> None:
    """Write the ledger as JSONL, one event object (with its hash) per line."""
    path = Path(path)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for event in events:
            f.write(_canonical(_event_payload(event, include_hash=True)))
            f.write("\n")


def verify_ledger(events: list[InjectionEvent]) -> bool:
    """Recompute the hash chain; return ``False`` on any tampering.

    Detects an edited field (recomputed hash diverges), a reordered pair, and a
    removed interior event (the following event's ``prev_hash`` no longer links).
    """
    prev_hash = ZERO_HASH
    for event in events:
        if event.prev_hash != prev_hash:
            return False
        if _compute_hash(event) != event.hash:
            return False
        prev_hash = event.hash
    return True
