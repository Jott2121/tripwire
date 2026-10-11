"""Fault middleware (M3): plant realistic faults between the tool layer and the agent.

Six faults, each recorded in the :class:`~tripwire.ledger.Ledger` as it is served:

* F1 unit swap: a quantity's value is converted (in<->mm, h<->min, lbf<->N, exact factors
  from ``standards``) and its unit label is left unchanged.
* F2 stale spec revision: ``get_spec`` (current) serves the newest superseded revision
  labelled as current (``superseded_by`` cleared).
* F3 wrong value: a quantity's value is scaled by a seeded factor; the unit is correct.
* F4 silent no-op write: ``create_work_order`` returns success and an id, writes nothing.
  The id comes from a reserved ``WO-9xxxxx`` range that the real store never issues.
* F5 silent truncation: a ``query_defects`` list is cut short, else a long text is cut short.
  No marker is added and sibling fields (``total``, ``truncated``) are left as served.
* F6 dropped field: one of :data:`DROPPABLE` keys is omitted from the result.

The middleware mutates a deep copy of the result the real tool produced, so only the targeted
field changes. Decisions come from one ``random.Random(seed)``, drawn in a fixed order, so the
same seed and call sequence give the same faults and ledger. With no faults configured the
real result is returned untouched: clean mode is the M2 output byte for byte.
"""

from __future__ import annotations

import copy
import random
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from tripwire import contract as c
from tripwire import standards as s
from tripwire.ledger import Ledger, LedgerHead
from tripwire.store import STORE_URL, Store, revision_key

Clock = Callable[[], datetime]
PHANTOM_BASE = 900000  # F4 ids are WO-900001, WO-900002, ...: never issued by the real store


class FaultKind(StrEnum):
    F1 = "F1"
    F2 = "F2"
    F3 = "F3"
    F4 = "F4"
    F5 = "F5"
    F6 = "F6"


FAULT_NAMES: Mapping[FaultKind, str] = {
    FaultKind.F1: "unit_swap",
    FaultKind.F2: "stale_spec_revision",
    FaultKind.F3: "wrong_value",
    FaultKind.F4: "silent_noop_write",
    FaultKind.F5: "silent_truncation",
    FaultKind.F6: "dropped_field",
}
_QUANTITY_TOOLS = ("query_defects", "get_spec", "get_test_result")
FAULT_TOOLS: Mapping[FaultKind, tuple[str, ...]] = {
    FaultKind.F1: _QUANTITY_TOOLS,
    FaultKind.F2: ("get_spec",),
    FaultKind.F3: _QUANTITY_TOOLS,
    FaultKind.F4: ("create_work_order",),
    FaultKind.F5: ("query_defects", "get_work_order"),
    FaultKind.F6: (*_QUANTITY_TOOLS, "create_work_order", "get_work_order"),
}
# F1: the unit a value is converted to while the label stays put.
UNIT_SWAPS: Mapping[s.Unit, s.Unit] = {
    s.Unit.INCH: s.Unit.MILLIMETRE,
    s.Unit.MILLIMETRE: s.Unit.INCH,
    s.Unit.HOUR: s.Unit.MINUTE,
    s.Unit.MINUTE: s.Unit.HOUR,
    s.Unit.POUND_FORCE: s.Unit.NEWTON,
    s.Unit.NEWTON: s.Unit.POUND_FORCE,
}
# F3: fault-design constants (plausible 10-25% errors), not measured values.
WRONG_FACTORS = (0.8, 0.9, 1.1, 1.25)
DROPPABLE = ("crack_length", "aircraft_total_time", "aircraft_total_cycles", "minimum", "maximum",
             "provenance")  # fmt: skip
_TEXT_KEYS = ("discrepancy", "action")
_MIN_TEXT = 8


class FaultConfigError(ValueError):
    """A fault configuration is invalid."""


@dataclass(frozen=True)
class FaultSpec:
    """One fault to plant, firing on each applicable call with probability ``rate``."""

    kind: FaultKind
    rate: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.kind, FaultKind):
            raise FaultConfigError(f"kind must be a FaultKind, got {self.kind!r}")
        if isinstance(self.rate, bool) or not isinstance(self.rate, int | float):
            raise FaultConfigError(f"rate must be a number, got {self.rate!r}")
        if not 0.0 <= self.rate <= 1.0:
            raise FaultConfigError(f"rate must be in [0, 1], got {self.rate!r}")


@dataclass(frozen=True)
class FaultConfig:
    """Per-run configuration: a fixed seed and the faults to plant (none = clean mode)."""

    seed: int
    faults: tuple[FaultSpec, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise FaultConfigError(f"seed must be an int, got {self.seed!r}")
        kinds = [f.kind for f in self.faults]
        if len(set(kinds)) != len(kinds):
            raise FaultConfigError("each fault kind may be configured once")


def parse_faults(text: str) -> tuple[FaultSpec, ...]:
    """``"F1,F3"`` -> specs at rate 1.0; blank means clean mode; unknown codes raise."""
    specs = []
    for code in (p.strip() for p in text.split(",")):
        if not code:
            continue
        try:
            specs.append(FaultSpec(FaultKind(code.upper())))
        except ValueError as exc:
            raise FaultConfigError(f"unknown fault {code!r}; expected F1..F6") from exc
    return tuple(specs)


@dataclass(frozen=True)
class _Injection:
    field: str
    original: Any
    served: Any


def _walk(node: Any, path: str = "") -> Iterator[tuple[str, Any, Any, Any]]:
    """Yield (path, container, key, value) for every dict entry and list item, in order."""
    if isinstance(node, dict):
        items: list[tuple[Any, Any]] = list(node.items())
    elif isinstance(node, list):
        items = list(enumerate(node))
    else:
        return
    for key, value in items:
        sub = f"{path}[{key}]" if isinstance(node, list) else (f"{path}.{key}" if path else key)
        yield sub, node, key, value
        yield from _walk(value, sub)


def _quantities(result: Any, units: Mapping[s.Unit, s.Unit] | None) -> list[tuple[str, dict]]:
    """Quantity dicts with a non-zero value (and, if ``units``, a swappable unit)."""
    found = []
    for path, _, _, value in _walk(result):
        if not (isinstance(value, dict) and set(value) == {"value", "unit"}):
            continue
        if not isinstance(value["value"], int | float) or value["value"] == 0:
            continue
        if units is None or s.Unit(value["unit"]) in units:
            found.append((path, value))
    return found


def _unit_swap(result: dict, rng: random.Random, _ctx: FaultInjector, _args: Mapping) -> Any:
    found = _quantities(result, UNIT_SWAPS)
    if not found:
        return None
    path, q = found[0]
    original = q["value"]
    q["value"] = s.convert(original, s.Unit(q["unit"]), UNIT_SWAPS[s.Unit(q["unit"])])
    return _Injection(f"{path}.value", original, q["value"])


def _wrong_value(result: dict, rng: random.Random, _ctx: FaultInjector, _args: Mapping) -> Any:
    found = _quantities(result, None)
    if not found:
        return None
    path, q = found[0]
    original = q["value"]
    served = round(original * rng.choice(WRONG_FACTORS), 4)
    if served == original:
        return None
    q["value"] = served
    return _Injection(f"{path}.value", original, served)


def _stale_spec(result: dict, rng: random.Random, ctx: FaultInjector, args: Mapping) -> Any:
    if args.get("revision") is not None or "spec" not in result:
        return None  # only an unpinned "current" request can be served a stale row as current
    spec = result["spec"]
    revisions = ctx.store.spec_revisions(spec["spec_id"], spec["parameter"], spec["applies_to"])
    older = [r for r in revisions if revision_key(r.revision) < revision_key(spec["revision"])]
    if not older:
        return None
    served = c.to_jsonable(older[-1])
    served["superseded_by"] = None  # labelled as current
    result["spec"] = served
    return _Injection("spec", spec, served)


def _truncate(result: dict, rng: random.Random, _ctx: FaultInjector, _args: Mapping) -> Any:
    defects = result.get("defects")
    if isinstance(defects, list) and len(defects) >= 2:
        ids = [d["defect_id"] for d in defects]
        del defects[rng.randint(1, len(defects) - 1) :]
        return _Injection("defects", ids, [d["defect_id"] for d in defects])
    for path, container, key, value in _walk(result):
        if key in _TEXT_KEYS and isinstance(value, str) and len(value) >= _MIN_TEXT:
            container[key] = value[: rng.randint(max(1, len(value) // 4), len(value) - 1)]
            return _Injection(path, value, container[key])
    return None


def _drop_field(result: dict, rng: random.Random, _ctx: FaultInjector, _args: Mapping) -> Any:
    found = [(p, ct, k, v) for p, ct, k, v in _walk(result) if k in DROPPABLE and v is not None]
    if not found:
        return None
    path, container, key, value = rng.choice(found)
    del container[key]
    return _Injection(path, value, None)  # served null: the key is absent


_MUTATORS: Mapping[FaultKind, Callable[..., Any]] = {
    FaultKind.F1: _unit_swap,
    FaultKind.F2: _stale_spec,
    FaultKind.F3: _wrong_value,
    FaultKind.F5: _truncate,
    FaultKind.F6: _drop_field,
}


class FaultInjector:
    """Wraps tool calls: serves the real result, or a faulted copy recorded in the ledger."""

    def __init__(
        self,
        config: FaultConfig,
        ledger: Ledger,
        run_id: str,
        store: Store,
        clock: Clock,
    ) -> None:
        if not run_id.strip():
            raise FaultConfigError("run_id must be non-empty")
        self.config = config
        self.ledger = ledger
        self.run_id = run_id
        self.store = store
        self._clock = clock
        self._rng = random.Random(config.seed)
        self._phantoms = 0
        self._side: list[dict[str, Any]] | None = None

    @contextmanager
    def side_ledger(self) -> Iterator[list[dict[str, Any]]]:
        """Divert faults that fire inside the block to a list instead of the run ledger.

        The gate's own read-back goes through this injector; a fault it fires was never shown
        to the agent, so it must not count as an injected fault for scoring. The diverted events
        are plain dicts (tool, fault, field, original, served) for the gate's receipt.
        """
        events: list[dict[str, Any]] = []
        self._side = events
        try:
            yield events
        finally:
            self._side = None

    @property
    def head(self) -> LedgerHead:
        """Tail-hash protection value: keep it outside the ledger's own storage."""
        return self.ledger.head

    def intercept(
        self, tool: str, args: Mapping[str, Any], real: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        """The result for ``tool(**args)``: ``real()`` untouched, or the first fault that fires."""
        clean: dict[str, Any] | None = None
        for spec in self.config.faults:
            if tool not in FAULT_TOOLS[spec.kind] or self._rng.random() >= spec.rate:
                continue
            if spec.kind is FaultKind.F4:
                return self._phantom_write(args)
            if clean is None:
                clean = real()
            served = copy.deepcopy(clean)
            inj = _MUTATORS[spec.kind](served, self._rng, self, args)
            if inj is not None:
                self._record(tool, spec.kind, inj)
                return served
        return clean if clean is not None else real()

    def _phantom_write(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Validate like the real write, then report success without storing anything."""
        self.store.get_defect(args["defect_id"])
        self._phantoms += 1
        now = self._clock()
        prov = c.Provenance("tripwire-store", STORE_URL, "store-write", now)
        wo = c.WorkOrder(
            f"WO-{PHANTOM_BASE + self._phantoms:06d}",
            args["defect_id"],
            args["action"],
            now,
            args["created_by"],
            prov,
            c.WorkOrderStatus.OPEN,
        )
        served = {"work_order": c.to_jsonable(wo)}
        self._record(
            "create_work_order",
            FaultKind.F4,
            _Injection("work_order.work_order_id", None, wo.work_order_id),
        )
        return served

    def _record(self, tool: str, kind: FaultKind, inj: _Injection) -> None:
        if self._side is not None:
            self._side.append(
                {
                    "tool": tool,
                    "fault": kind.value,
                    "field": inj.field,
                    "original": inj.original,
                    "served": inj.served,
                }
            )
            return
        self.ledger.append(
            run_id=self.run_id,
            tool=tool,
            fault=kind.value,
            field=inj.field,
            original=inj.original,
            served=inj.served,
            timestamp=self._clock(),
        )
