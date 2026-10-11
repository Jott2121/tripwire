"""Data contract (M1): every value Tripwire serves carries unit, revision, provenance.

A :class:`Quantity` is a finite number with a :class:`standards.Unit`, converted
only via :func:`standards.convert`. Every record carries a :class:`Provenance`
and can be fingerprinted (sha256 of canonical JSON) so a write and its
read-back compare byte for byte. Validation runs in ``__post_init__`` and
raises :class:`ContractError`: an invalid record cannot be constructed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from tripwire import standards as s


class ContractError(ValueError):
    """A value or record violates the data contract."""


def _nonempty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} must be a non-empty string")
    return value


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ContractError(f"{name} must be a timezone-aware datetime")
    return value


@dataclass(frozen=True)
class Provenance:
    """Where a record came from and which revision of the source it reflects."""

    source: str  # dataset or system id, e.g. "faa-sdrs"
    source_url: str
    revision: (
        str  # source revision, e.g. "SDR-2026 retrieved 2026-10-11" or "NASA-STD-8739.4A chg4"
    )
    retrieved_at: datetime  # tz-aware

    def __post_init__(self) -> None:
        _nonempty(self.source, "source")
        _nonempty(self.source_url, "source_url")
        _nonempty(self.revision, "revision")
        _aware(self.retrieved_at, "retrieved_at")


@dataclass(frozen=True)
class Quantity:
    """A finite number with a unit. The unit is never implied."""

    value: float
    unit: s.Unit

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int | float):
            raise ContractError(f"value must be a number, got {type(self.value).__name__}")
        if not math.isfinite(self.value):
            raise ContractError(f"value must be finite, got {self.value!r}")
        if not isinstance(self.unit, s.Unit):
            raise ContractError(f"unit must be a standards.Unit, got {self.unit!r}")

    @property
    def dimension(self) -> s.Dimension:
        return s.UNIT_DIMENSION[self.unit]

    def to(self, unit: s.Unit) -> Quantity:
        """Convert via the defined factors; a cross-dimension request raises."""
        try:
            return Quantity(s.convert(self.value, self.unit, unit), unit)
        except s.StandardsError as exc:
            raise ContractError(str(exc)) from exc


@dataclass(frozen=True)
class Defect:
    """A service-difficulty style defect record (SDR-shaped, synthetic or public)."""

    defect_id: str
    difficulty_date: date
    submission_date: datetime  # tz-aware
    jasc_code: str
    registry: str  # SDR convention: no leading N
    part_number: str
    part_condition: str
    discrepancy: str
    provenance: Provenance
    crack_length: Quantity | None = None  # inches in the source
    aircraft_total_time: Quantity | None = None  # hours
    aircraft_total_cycles: Quantity | None = None  # cycles

    def __post_init__(self) -> None:
        _nonempty(self.defect_id, "defect_id")
        _nonempty(self.part_number, "part_number")
        _aware(self.submission_date, "submission_date")
        try:
            s.jasc_system(self.jasc_code)
            if self.registry:
                s.parse_n_number(self.registry, prefix=False)
        except s.StandardsError as exc:
            raise ContractError(str(exc)) from exc
        if not s.difficulty_precedes_submission(self.difficulty_date, self.submission_date):
            raise ContractError("difficulty_date is after submission_date")
        for name, q, dim in (
            ("crack_length", self.crack_length, s.Dimension.LENGTH),
            ("aircraft_total_time", self.aircraft_total_time, s.Dimension.TIME_IN_SERVICE),
            ("aircraft_total_cycles", self.aircraft_total_cycles, s.Dimension.CYCLES),
        ):
            if q is not None and q.dimension is not dim:
                raise ContractError(f"{name} must be a {dim.value} quantity, got {q.unit.value}")


@dataclass(frozen=True)
class SpecLimit:
    """One acceptance limit from a cited public standard, at a stated revision."""

    spec_id: str  # e.g. "NASA-STD-8739.4A"
    revision: str  # e.g. "chg4"
    parameter: str  # e.g. "crimp_pull_min"
    applies_to: str  # e.g. "AWG 22"
    source_url: str
    provenance: Provenance
    minimum: Quantity | None = None
    maximum: Quantity | None = None
    superseded_by: str | None = None  # revision that replaces this one, if any

    def __post_init__(self) -> None:
        for name in ("spec_id", "revision", "parameter", "applies_to", "source_url"):
            _nonempty(getattr(self, name), name)
        if self.minimum is None and self.maximum is None:
            raise ContractError("a spec limit needs a minimum, a maximum, or both")
        if self.minimum is not None and self.maximum is not None:
            if self.minimum.dimension is not self.maximum.dimension:
                raise ContractError("minimum and maximum have different dimensions")
            if self.minimum.to(self.maximum.unit).value > self.maximum.value:
                raise ContractError("minimum exceeds maximum")

    @property
    def unit(self) -> s.Unit:
        bound = self.minimum if self.minimum is not None else self.maximum
        assert bound is not None  # guaranteed by __post_init__
        return bound.unit

    @property
    def is_current(self) -> bool:
        return self.superseded_by is None

    def check(self, measured: Quantity) -> bool:
        """True when ``measured`` lies within the limit, compared in the limit's unit.

        A cross-dimension comparison raises: the gate never guesses a unit.
        """
        m = measured.to(self.unit).value
        if self.minimum is not None and m < self.minimum.to(self.unit).value:
            return False
        return not (self.maximum is not None and m > self.maximum.to(self.unit).value)


@dataclass(frozen=True)
class Lot:
    """A received lot of parts (synthetic; fictional suppliers only)."""

    lot_id: str
    part_number: str
    quantity: int
    received_on: date
    supplier: str
    provenance: Provenance

    def __post_init__(self) -> None:
        _nonempty(self.lot_id, "lot_id")
        _nonempty(self.part_number, "part_number")
        _nonempty(self.supplier, "supplier")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int):
            raise ContractError("quantity must be an int")
        if self.quantity <= 0:
            raise ContractError("quantity must be positive")


@dataclass(frozen=True)
class TestResult:
    """A measurement taken on a lot against a named spec limit at a stated revision."""

    test_id: str
    lot_id: str
    parameter: str
    measured: Quantity
    spec_id: str
    spec_revision: str  # the revision the test was judged against
    tested_at: datetime  # tz-aware
    provenance: Provenance

    def __post_init__(self) -> None:
        for name in ("test_id", "lot_id", "parameter", "spec_id", "spec_revision"):
            _nonempty(getattr(self, name), name)
        _aware(self.tested_at, "tested_at")

    def judge(self, spec: SpecLimit) -> bool:
        """Pass/fail against ``spec``; refuses a mismatched or stale spec (fail closed)."""
        if spec.spec_id != self.spec_id or spec.parameter != self.parameter:
            raise ContractError("spec does not match this test's spec_id/parameter")
        if spec.revision != self.spec_revision:
            raise ContractError(
                f"spec revision {spec.revision} differs from the test's {self.spec_revision}"
            )
        if not spec.is_current:
            raise ContractError(
                f"spec revision {spec.revision} is superseded by {spec.superseded_by}"
            )
        return spec.check(self.measured)


class WorkOrderStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    CLOSED = "closed"


@dataclass(frozen=True)
class WorkOrder:
    """A maintenance work order raised against a defect."""

    work_order_id: str
    defect_id: str
    action: str
    created_at: datetime  # tz-aware
    created_by: str
    provenance: Provenance
    status: WorkOrderStatus = WorkOrderStatus.OPEN

    def __post_init__(self) -> None:
        for name in ("work_order_id", "defect_id", "action", "created_by"):
            _nonempty(getattr(self, name), name)
        _aware(self.created_at, "created_at")
        if not isinstance(self.status, WorkOrderStatus):
            raise ContractError(f"status must be a WorkOrderStatus, got {self.status!r}")


def to_jsonable(obj: Any) -> Any:
    """Canonical JSON-ready form: dataclasses -> dicts, enums -> values, dates -> ISO."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, StrEnum):
        return obj.value
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if isinstance(obj, list | tuple):
        return [to_jsonable(v) for v in obj]
    if obj is None or isinstance(obj, str | int | float | bool):
        return obj
    raise ContractError(f"cannot serialize {type(obj).__name__}")


def canonical_json(obj: Any) -> str:
    return json.dumps(to_jsonable(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def fingerprint(obj: Any) -> str:
    """sha256 of the canonical JSON; equal records have equal fingerprints."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
