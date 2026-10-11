"""In-memory store over a synthetic dataset: the MCP server's backing data.

Fails closed on inconsistent data (duplicate ids, orphan references, a spec without
exactly one current revision). Reads hand out frozen records and tuples.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, TypeVar

from tripwire import contract as c
from tripwire.contract import Defect, Lot, SpecLimit, TestResult, WorkOrder
from tripwire.synthdata import Dataset

STORE_URL = "https://github.com/Jott2121/tripwire/blob/main/tripwire/store.py"
_WO_ID = re.compile(r"WO-(\d{6})")
_T = TypeVar("_T")
SpecGroup = tuple[str, str, str]  # (spec_id, parameter, applies_to)


class NotFound(KeyError):
    """A requested record does not exist in the store."""


class StoreError(ValueError):
    """The store's data is inconsistent, or a request is invalid."""


@dataclass(frozen=True)
class QueryResult:
    """One page of defects plus the full match count, so truncation is visible."""

    defects: tuple[Defect, ...]
    total: int
    truncated: bool


def spec_row(sp: SpecLimit) -> tuple[str, str, str, str]:
    """(spec_id, parameter, applies_to, revision): the identity of one spec row."""
    return (sp.spec_id, sp.parameter, sp.applies_to, sp.revision)


def spec_key(sp: SpecLimit) -> str:
    """Snapshot key of one spec limit revision."""
    return "|".join(spec_row(sp))


def _typed(records: Iterable[object], cls: type[_T]) -> tuple[_T, ...]:
    """Materialize ``records``, refusing anything that is not a ``cls``."""
    out: list[_T] = []
    for r in records:
        if not isinstance(r, cls):
            raise StoreError(f"expected {cls.__name__} records, got {type(r).__name__}")
        out.append(r)
    return tuple(out)


def _group_specs(specs: Iterable[SpecLimit]) -> dict[SpecGroup, tuple[SpecLimit, ...]]:
    """Group revisions by (spec_id, parameter, applies_to); each needs exactly one current."""
    groups: dict[SpecGroup, list[SpecLimit]] = {}
    for sp in specs:
        groups.setdefault((sp.spec_id, sp.parameter, sp.applies_to), []).append(sp)
    for key, revs in groups.items():
        n_current = sum(sp.is_current for sp in revs)
        if n_current != 1:
            raise StoreError(f"{'|'.join(key)} has {n_current} current revisions, need exactly 1")
    return {k: tuple(sorted(v, key=lambda sp: sp.revision)) for k, v in groups.items()}


def _matches(
    d: Defect, jasc: str | None, pn: str | None, reg: str | None, since: date | None
) -> bool:
    """True when ``d`` passes every given filter (filters are ANDed)."""
    return (
        (jasc is None or d.jasc_code == jasc)
        and (pn is None or d.part_number == pn)
        and (reg is None or d.registry == reg)
        and (since is None or d.difficulty_date >= since)
    )


def _get(table: dict[str, _T], key: str, kind: str) -> _T:
    """Look ``key`` up in ``table``; NotFound on a miss."""
    if key not in table:
        raise NotFound(f"no {kind} {key!r}")
    return table[key]


class Store:
    """In-memory, fail-closed view of one dataset plus the work orders written to it."""

    def __init__(
        self,
        defects: Iterable[Defect],
        lots: Iterable[Lot],
        spec_limits: Iterable[SpecLimit],
        test_results: Iterable[TestResult],
        work_orders: Iterable[WorkOrder],
    ) -> None:
        self._ids: set[str] = set()
        self._defects = {self._claim(r.defect_id): r for r in _typed(defects, Defect)}
        self._lots = {self._claim(r.lot_id): r for r in _typed(lots, Lot)}
        self._specs = {self._claim(spec_key(r)): r for r in _typed(spec_limits, SpecLimit)}
        self._tests = {self._claim(r.test_id): r for r in _typed(test_results, TestResult)}
        self._orders = {self._claim(r.work_order_id): r for r in _typed(work_orders, WorkOrder)}
        self._tables: dict[str, dict[str, Any]] = {  # every collection, keyed by record id
            "defects": self._defects,
            "lots": self._lots,
            "spec_limits": self._specs,
            "test_results": self._tests,
            "work_orders": self._orders,
        }
        self._groups = _group_specs(self._specs.values())
        by_date = sorted(self._defects.values(), key=lambda d: (d.submission_date, d.defect_id))
        self._by_date = tuple(by_date)
        self._check_references()
        seqs = (int(m.group(1)) for k in self._orders if (m := _WO_ID.fullmatch(k)))
        self._wo_seq = max(seqs, default=0)

    @classmethod
    def from_dataset(cls, ds: Dataset) -> Store:
        """Build a store from a generated dataset."""
        return cls(ds.defects, ds.lots, ds.spec_limits, ds.test_results, ds.work_orders)

    def _claim(self, record_id: str) -> str:
        """Reserve ``record_id`` across all collections; a duplicate raises StoreError."""
        if record_id in self._ids:
            raise StoreError(f"duplicate id {record_id!r}")
        self._ids.add(record_id)
        return record_id

    def _check_references(self) -> None:
        """Every test resolves to a lot and a spec revision; every work order to a defect."""
        # Spec keys are unique (a duplicate fails in _claim), so membership means exactly one row.
        rows = {spec_row(sp) for sp in self._specs.values()}
        for t in self._tests.values():
            if t.lot_id not in self._lots:
                raise StoreError(f"test {t.test_id} references unknown lot {t.lot_id!r}")
            row = (t.spec_id, t.parameter, t.applies_to, t.spec_revision)
            if row not in rows:
                raise StoreError(f"test {t.test_id} resolves to no spec row: {'|'.join(row)}")
        for wo in self._orders.values():
            if wo.defect_id not in self._defects:
                raise StoreError(f"work order {wo.work_order_id} references unknown defect")

    def query_defects(
        self,
        *,
        jasc_code: str | None = None,
        part_number: str | None = None,
        registry: str | None = None,
        since: date | None = None,
        limit: int = 50,
    ) -> QueryResult:
        """Defects matching all filters, ordered by (submission_date, defect_id)."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise StoreError(f"limit must be an int >= 1, got {limit!r}")
        if since is not None and (isinstance(since, datetime) or not isinstance(since, date)):
            raise StoreError(f"since must be a date, got {since!r}")
        hits = [d for d in self._by_date if _matches(d, jasc_code, part_number, registry, since)]
        return QueryResult(tuple(hits[:limit]), len(hits), len(hits) > limit)

    def spec_revisions(
        self, spec_id: str, parameter: str, applies_to: str
    ) -> tuple[SpecLimit, ...]:
        """Every revision of one spec limit, sorted by revision string."""
        key = (spec_id, parameter, applies_to)
        if key not in self._groups:
            raise NotFound(f"no spec {'|'.join(key)}")
        return self._groups[key]

    def get_spec(
        self, spec_id: str, parameter: str, applies_to: str, revision: str | None = None
    ) -> SpecLimit:
        """The current revision, or exactly ``revision`` (superseded ones included)."""
        for sp in self.spec_revisions(spec_id, parameter, applies_to):
            wanted = sp.is_current if revision is None else sp.revision == revision
            if wanted:
                return sp
        raise NotFound(f"no revision {revision!r} of {spec_id}|{parameter}|{applies_to}")

    def get_test_result(self, test_id: str) -> TestResult:
        """One test result by id."""
        return _get(self._tests, test_id, "test result")

    def get_lot(self, lot_id: str) -> Lot:
        """One lot by id."""
        return _get(self._lots, lot_id, "lot")

    def get_defect(self, defect_id: str) -> Defect:
        """One defect by id."""
        return _get(self._defects, defect_id, "defect")

    def get_work_order(self, work_order_id: str) -> WorkOrder:
        """One work order by id."""
        return _get(self._orders, work_order_id, "work order")

    def create_work_order(
        self, defect_id: str, action: str, created_by: str, now: datetime
    ) -> WorkOrder:
        """Create and store an OPEN work order; a failed create consumes no id."""
        self.get_defect(defect_id)
        n = self._wo_seq + 1
        while f"WO-{n:06d}" in self._ids:  # never reuse an id held by another collection
            n += 1
        # Both constructors validate (tz-aware now, non-empty fields) before the counter moves.
        prov = c.Provenance("tripwire-store", STORE_URL, "store-write", now)
        wo = WorkOrder(
            f"WO-{n:06d}", defect_id, action, now, created_by, prov, c.WorkOrderStatus.OPEN
        )
        self._orders[self._claim(wo.work_order_id)] = wo
        self._wo_seq = n
        return wo

    def snapshot(self) -> dict[str, str]:
        """Every record id mapped to its fingerprint, keys sorted."""
        records = {k: v for table in self._tables.values() for k, v in table.items()}
        return {k: c.fingerprint(records[k]) for k in sorted(records)}

    def counts(self) -> dict[str, int]:
        """Number of records per collection."""
        return {name: len(table) for name, table in self._tables.items()}
