"""tripwire.store: fail-closed construction, deterministic queries, honest writes."""

import dataclasses
from datetime import UTC, date, datetime

import pytest

from tripwire import contract as c
from tripwire import synthdata as sd
from tripwire.store import NotFound, QueryResult, Store, StoreError

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
URL = "https://github.com/Jott2121/tripwire/blob/main/tripwire/store.py"
BRACKET = ("TW-SRM-PN-10001", "crack_length_max", "BRACKET PN-10001")
ARGS = ("replace part", "agent-01", NOW)


DS = sd.generate(7, n_defects=60)  # frozen, so safe to share


@pytest.fixture
def store() -> Store:
    return Store.from_dataset(DS)


def _parts(ds: sd.Dataset) -> dict[str, tuple]:
    return {f.name: getattr(ds, f.name) for f in dataclasses.fields(ds) if f.name != "seed"}


def test_from_dataset_counts_match(store):
    assert store.counts() == {k: len(v) for k, v in _parts(DS).items()}
    assert Store((), (), (), (), ()).query_defects() == QueryResult((), 0, False)


def test_query_filters_individually_and_combined(store):
    d0 = DS.defects[0]
    for field in ("jasc_code", "part_number", "registry"):
        want = {d.defect_id for d in DS.defects if getattr(d, field) == getattr(d0, field)}
        got = store.query_defects(**{field: getattr(d0, field)}, limit=100)
        assert {d.defect_id for d in got.defects} == want and got.total == len(want)
    both = store.query_defects(part_number=d0.part_number, jasc_code=d0.jasc_code, limit=100)
    want = [d for d in DS.defects if (d.part_number, d.jasc_code) == (d0.part_number, d0.jasc_code)]
    assert d0 in both.defects and set(both.defects) == set(want)
    assert store.query_defects(part_number=d0.part_number, registry="no-such").total == 0


def test_since_filters_on_difficulty_date_inclusively(store):
    cut = sorted(d.difficulty_date for d in DS.defects)[len(DS.defects) // 2]
    got = store.query_defects(since=cut, part_number=DS.defects[0].part_number, limit=100)
    want = {d for d in DS.defects if d.difficulty_date >= cut}
    assert set(got.defects) == {d for d in want if d.part_number == DS.defects[0].part_number}
    assert store.query_defects(since=cut, limit=100).total == len(want)
    assert store.query_defects(since=date(2030, 1, 1)).total == 0
    for bad in (NOW, "2026-01-01"):  # a datetime or string is not a date filter
        with pytest.raises(StoreError):
            store.query_defects(since=bad)


def test_order_is_deterministic_and_independent_of_input_order(store):
    got = store.query_defects(limit=100).defects
    keys = [(d.submission_date, d.defect_id) for d in got]
    assert keys == sorted(keys) and isinstance(got, tuple)
    rev = Store(*(tuple(reversed(v)) for v in _parts(DS).values()))
    assert rev.query_defects(limit=100) == store.query_defects(limit=100)
    assert Store.from_dataset(sd.generate(7, n_defects=60)).snapshot() == store.snapshot()


def test_limit_total_and_truncated(store):
    full = store.query_defects(limit=60)
    assert (full.total, full.truncated, len(full.defects)) == (60, False, 60)
    page = store.query_defects(limit=5)
    assert (page.total, page.truncated, page.defects) == (60, True, full.defects[:5])
    default = store.query_defects()
    assert (default.total, default.truncated, len(default.defects)) == (60, True, 50)
    assert store.query_defects(limit=1000).truncated is False
    with pytest.raises(dataclasses.FrozenInstanceError):
        page.total = 5
    for bad in (0, -1, True, 1.5, None):
        with pytest.raises(StoreError):
            store.query_defects(limit=bad)


def test_get_spec_current_explicit_revision_and_not_found(store):
    current = store.get_spec(*BRACKET)
    assert current.revision == "r2" and current.is_current
    assert store.get_spec(*BRACKET, revision="r1").superseded_by == "r2"
    assert store.get_spec(*BRACKET, revision="r2") is current
    assert store.get_spec("NASA-STD-8739.4A", "crimp_tensile_min", "AWG 22").revision == "chg4"
    for args, rev in ((BRACKET, "r9"), (("TW-SRM-NOPE", *BRACKET[1:]), None)):
        with pytest.raises(NotFound):
            store.get_spec(*args, revision=rev)
    with pytest.raises(NotFound):
        store.spec_revisions("TW-SRM-NOPE", "x", "y")


def test_spec_revisions_sorted_by_revision():
    revs = Store(**{**_parts(DS), "spec_limits": DS.spec_limits[::-1]}).spec_revisions(*BRACKET)
    assert isinstance(revs, tuple) and [r.revision for r in revs] == ["r1", "r2"]


def test_getters_return_records_and_raise_not_found(store):
    assert store.get_defect(DS.defects[3].defect_id) is DS.defects[3]
    assert store.get_lot(DS.lots[0].lot_id) is DS.lots[0]
    assert store.get_test_result(DS.test_results[0].test_id) is DS.test_results[0]
    assert store.get_work_order(DS.work_orders[0].work_order_id) is DS.work_orders[0]
    for getter in (store.get_defect, store.get_lot, store.get_test_result, store.get_work_order):
        with pytest.raises(NotFound):
            getter("nope")


def test_create_work_order_happy_path(store):
    existing, d = set(store.snapshot()), DS.defects[10]
    wo = store.create_work_order(d.defect_id, *ARGS)
    assert wo.work_order_id == "WO-000001" and wo.work_order_id not in existing
    assert (wo.defect_id, wo.action, wo.created_by, wo.created_at) == (d.defect_id, *ARGS)
    assert wo.status is c.WorkOrderStatus.OPEN
    assert wo.provenance == c.Provenance("tripwire-store", URL, "store-write", NOW)
    assert store.get_work_order(wo.work_order_id) is wo and store.counts()["work_orders"] == 6


def test_failed_creates_consume_no_id_and_identical_creates_give_two(store):
    before, d_id = store.snapshot(), DS.defects[0].defect_id
    with pytest.raises(NotFound):
        store.create_work_order("no-such-defect", *ARGS)
    with pytest.raises(c.ContractError):
        store.create_work_order(d_id, "replace part", "agent-01", NOW.replace(tzinfo=None))
    with pytest.raises(c.ContractError):
        store.create_work_order(d_id, " ", "agent-01", NOW)
    assert store.snapshot() == before
    a, b = (store.create_work_order(d_id, "quarantine lot", "agent-01", NOW) for _ in range(2))
    assert (a.work_order_id, b.work_order_id) == ("WO-000001", "WO-000002")
    assert store.get_work_order("WO-000001") is a and store.get_work_order("WO-000002") is b


def test_sequence_starts_after_existing_ids_of_that_shape():
    wos = (dataclasses.replace(DS.work_orders[0], work_order_id="WO-000041"),)
    lots = (*DS.lots, dataclasses.replace(DS.lots[0], lot_id="WO-000042"))
    st = Store(DS.defects, lots, DS.spec_limits, DS.test_results, wos)
    assert st.create_work_order(DS.defects[0].defect_id, "x", "a", NOW).work_order_id == "WO-000043"


def test_snapshot_changes_only_by_the_new_key(store):
    before = store.snapshot()
    assert list(before) == sorted(before)
    assert before["|".join((*BRACKET, "r2"))] == c.fingerprint(store.get_spec(*BRACKET))
    wo = store.create_work_order(store.query_defects().defects[0].defect_id, "x", "a", NOW)
    after = store.snapshot()
    assert set(after) - set(before) == {wo.work_order_id}
    assert {k: v for k, v in after.items() if k != wo.work_order_id} == before
    assert after[wo.work_order_id] == c.fingerprint(wo)


def _bad_inputs(ds: sd.Dataset) -> dict[str, tuple[dict[str, tuple], str]]:
    r, (t0, w0, lot0) = dataclasses.replace, (ds.test_results[0], ds.work_orders[0], ds.lots[0])
    r1, r2 = (sp for sp in ds.spec_limits if sp.spec_id == BRACKET[0])
    others = tuple(sp for sp in ds.spec_limits if sp not in (r1, r2))
    dup = "duplicate id"
    return {
        "dup defect": ({"defects": (*ds.defects, ds.defects[0])}, dup),
        "dup lot": ({"lots": (*ds.lots, lot0)}, dup),
        "dup spec": ({"spec_limits": (*ds.spec_limits, r1)}, dup),
        "dup test": ({"test_results": (*ds.test_results, t0)}, dup),
        "dup work order": ({"work_orders": (*ds.work_orders, w0)}, dup),
        "id in two collections": ({"lots": (r(lot0, lot_id=ds.defects[0].defect_id),)}, dup),
        "orphan test lot": ({"test_results": (r(t0, lot_id="LOT-missing"),)}, "unknown lot"),
        "orphan test spec": ({"test_results": (r(t0, spec_id="TW-SRM-NOPE"),)}, "unknown spec"),
        "orphan test param": ({"test_results": (r(t0, parameter="other"),)}, "unknown spec"),
        "orphan test rev": ({"test_results": (r(t0, spec_revision="r9"),)}, "unknown spec"),
        "orphan work order": ({"work_orders": (r(w0, defect_id="no"),)}, "unknown defect"),
        "zero current": ({"spec_limits": (*others, r1, r(r2, superseded_by="r3"))}, "0 current"),
        "two current": ({"spec_limits": (*others, r(r1, superseded_by=None), r2)}, "2 current"),
        "wrong type": ({"lots": (ds.defects[0],)}, "expected Lot"),
    }


@pytest.mark.parametrize("case", list(_bad_inputs(DS)))
def test_constructor_fails_closed(case):
    override, reason = _bad_inputs(DS)[case]
    with pytest.raises(StoreError, match=reason):
        Store(**{**_parts(DS), **override})
