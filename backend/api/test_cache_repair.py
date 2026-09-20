"""#807 — repairing a durable cache whose order plane disagrees with the venue.

EVERY BYTE UNDER TEST IS PRODUCTION'S. `fixtures/repair_807/redis.json` is the paper stack's Redis on
2026-09-09 06:50 UTC (orders, positions, instruments, index sets for the fifteen instruments the
corpses touched), and `venue.json` is Alpaca's answer for every REJECTED-for-a-corpse-reason order
(68 asked, 45 known, 23 never heard of) and the account's positions at the same moment. A hand-built double could not have represented this defect: the
inferred fill that reuses trade id `5641a8b1` with a different quantity, the `reconciliation=True`
flag on every lane fill (paper has no trade-updates socket), or a lane's SHORT under NETTING.

THE FIXTURE MUST BE ABLE TO EXPRESS THE BUG BEFORE ANY INVARIANCE IS ASSERTED (CLAUDE.md). The
first tests pin the broken state; only then do the plan tests mean anything.
"""
from __future__ import annotations

import base64
import copy
import fnmatch
import json
from decimal import Decimal
from pathlib import Path

import pytest

from api import cache_repair as cr

# READS A PRODUCTION CAPTURE (#1045): skipped by name where api/fixtures/repair_807/ is absent (public tree).
pytestmark = pytest.mark.captured_fixture("repair_807")

FIXTURES = Path(__file__).parent / "fixtures" / "repair_807"

PATH_CORPSE = "PROT-SELL-PATH-XNYS-b3ea77ee"
CRAK_CORPSE = "PROT-SELL-CRAK-ARCX-464b1139"
PATH_LANE = "PATH.XNYS-TECHIVOL-005"
#: The thirteen instruments where a corpse FIRED (venue `filled`) and the two where one still rests.
#: Eight of the thirteen (ADP, APA, SSRM, TOST, U, VEEV, ZETA, ZS) had fired BEFORE #801 was written and
#: never made its list; their fills were the `Cannot open NETTING position … reduce-only` lines at boot.
FIRED = {"CRM.XNYS", "HALO.XNAS", "LAND.XNAS", "PATH.XNYS", "WDAY.XNAS", "ADP.XNAS", "APA.XNAS", "SSRM.XNAS",
         "TOST.XNYS", "U.XNYS", "VEEV.XNYS", "ZETA.XNYS", "ZS.XNAS"}
RESTING = {"CRAK.ARCX", "PAGP.XNAS"}


@pytest.fixture(scope="module")
def image() -> cr.CacheImage:
    return cr.CacheImage.from_fixture(FIXTURES / "redis.json")


@pytest.fixture(scope="module")
def venue() -> cr.VenueTruth:
    return cr.VenueTruth.from_fixture(FIXTURES / "venue.json")


@pytest.fixture(scope="module")
def plan(image, venue) -> cr.RepairPlan:
    return cr.plan_repair(image, venue)


def _net_of(instrument, events: list[bytes]) -> Decimal:
    instances = cr.replay_position(instrument, [cr.to_obj(e) for e in events])
    last = instances[-1] if instances else None
    return Decimal(str(last.signed_qty)) if last is not None and not last.is_closed else Decimal(0)


# --------------------------------------------------------------------------------------------------
# The fixture can express the bug
# --------------------------------------------------------------------------------------------------


def test_fixture_the_corpse_replays_REJECTED_for_a_reason_that_means_unheard(image):
    order = cr.replay_order(image.orders[PATH_CORPSE])
    assert order.status_string() == "REJECTED"
    assert str(order.filled_qty) == "0"  # the order plane never recorded a fill
    rejected = [cr.to_obj(e) for e in image.orders[PATH_CORPSE] if cr.to_dict(e)["type"] == "OrderRejected"]
    assert rejected and rejected[-1].reason == "ORDER_NOT_FOUND_AT_VENUE"


def test_fixture_the_venue_says_that_same_order_filled_112(venue):
    vo = venue.orders[PATH_CORPSE]
    assert vo.status == "filled" and vo.filled_qty == Decimal(112)
    assert venue.positions["PATH"] == Decimal(126)  # 112 sold, 126 re-bought the same day


def test_fixture_the_lane_position_replays_to_a_short_the_venue_never_held(image):
    """192 booked against 112 sold: 80 + 11 + 4 + 3 real, then 94 under the SAME trade id as the 80."""
    instrument = cr.to_obj(image.instruments["PATH.XNYS"])
    fills = [cr.to_dict(e) for e in image.positions[PATH_LANE]]
    assert [f["last_qty"] for f in fills] == ["80", "11", "4", "3", "94"]
    assert fills[0]["trade_id"] == fills[-1]["trade_id"], "the inferred duplicate reuses the live fill's trade id"
    assert _net_of(instrument, image.positions[PATH_LANE]) == Decimal(-192)


def test_fixture_the_serializer_round_trips_every_production_byte_unchanged(image):
    """The kernel's serializer (`system/kernel.py:317`) — anything else writes bytes the node cannot read."""
    n = 0
    for events in list(image.orders.values()) + list(image.positions.values()):
        for raw in events:
            assert cr.obj_to_bytes(cr.to_obj(raw)) == raw
            n += 1
    assert n > 500  # vacuity guard: the fixture is not empty


# --------------------------------------------------------------------------------------------------
# The order plane
# --------------------------------------------------------------------------------------------------


def test_a_resting_corpse_is_revived_to_ACCEPTED_with_no_fills(plan):
    rv = next(r for r in plan.revivals if r.client_order_id == CRAK_CORPSE)
    assert (rv.before, rv.after) == ("REJECTED", "ACCEPTED")
    assert rv.kept_fill_qty == 0 and rv.synthetic_fill_qty == 0
    assert [cr.to_dict(e)["type"] for e in rv.events] == ["OrderInitialized", "OrderSubmitted", "OrderAccepted"]
    assert cr.replay_order(rv.events).status_string() == "ACCEPTED"


def test_a_fired_corpse_is_revived_to_FILLED_with_exactly_the_venues_quantity(plan, venue):
    fired = [r for r in plan.revivals if venue.orders[r.client_order_id].status == "filled"]
    assert len(fired) == 13, "thirteen corpses fired — five #801 knew of, eight it did not"
    assert {r.instrument_id for r in fired} == FIRED
    for rv in fired:
        order = cr.replay_order(rv.events)
        assert order.status_string() == "FILLED", rv.client_order_id
        assert Decimal(str(order.filled_qty)) == venue.orders[rv.client_order_id].filled_qty
        assert rv.kept_fill_qty + rv.synthetic_fill_qty == venue.orders[rv.client_order_id].filled_qty


def test_the_inferred_duplicate_is_not_kept_and_the_remainder_is_one_marked_fill(plan, venue):
    """PATH: 80 + 11 + 4 + 3 = 98 real fills fit inside the venue's 112; the 94 would not. One synthetic
    14 closes the gap, flagged `reconciliation=True` (Nautilus's own flag for an unwitnessed fill)."""
    rv = next(r for r in plan.revivals if r.client_order_id == PATH_CORPSE)
    fills = [cr.to_dict(e) for e in rv.events if cr.to_dict(e)["type"] == "OrderFilled"]
    assert [f["last_qty"] for f in fills] == ["80", "11", "4", "3", "14"]
    assert len({f["trade_id"] for f in fills}) == 5, "no trade id twice"
    last = fills[-1]
    assert last["reconciliation"] is True
    assert last["trade_id"] == cr.synthetic_trade_id(venue.orders[PATH_CORPSE].venue_order_id)
    assert len(last["trade_id"]) == 36  # TradeId's own cap, measured: 40 was refused
    assert Decimal(last["last_px"]) == Decimal("15.78")  # the venue's 15.779464 at the instrument's precision
    assert rv.kept_fill_qty == 98 and rv.synthetic_fill_qty == 14


def test_on_a_trade_id_collision_the_live_fill_wins_whatever_the_list_order(image, venue):
    """PATH's 80 (live) and 94 (inferred) share `5641a8b1` and a timestamp. Reverse their order in the
    list and the kept quantity must still be the venue-witnessed 80, not the invented 94."""
    img = copy.deepcopy(image)
    ev = img.positions[PATH_LANE]
    img.positions[PATH_LANE] = [ev[-1]] + ev[:-1]
    rv = next(r for r in cr.plan_repair(img, venue).revivals if r.client_order_id == PATH_CORPSE)
    fills = [cr.to_dict(e) for e in rv.events if cr.to_dict(e)["type"] == "OrderFilled"]
    assert [f["last_qty"] for f in fills] == ["80", "11", "4", "3", "14"]


def test_the_running_node_never_allows_overfills(monkeypatch):
    """The synthetic remainder's protection against a later real venue trade for the same shares is
    Nautilus's overfill rejection — which only holds while `allow_overfills` stays False (codex review)."""
    monkeypatch.setenv("KUMO_DURABLE_CACHE", "1")
    from api.engine_node import _durable_configs
    _, exec_cfg = _durable_configs()
    assert exec_cfg is not None and exec_cfg.allow_overfills is False


def test_a_fresh_id_fill_past_the_venues_total_is_dropped_by_the_cap(image, venue):
    """The trade-id rule catches PATH's duplicate because it REUSED `5641a8b1`. Nautilus's inferred fills
    normally carry a fresh id — give the 94 one, and only the venue's own total can reject it. Two
    mechanisms, one fixture: without this the cap was dead code that a mutation could not kill."""
    img = copy.deepcopy(image)
    events = img.positions[PATH_LANE]
    d = cr.to_dict(events[-1])
    assert d["last_qty"] == "94"
    d["trade_id"] = "00000000-0000-4000-8000-000000000094"
    events[-1] = cr.obj_to_bytes(cr.dict_to_obj(d))
    rv = next(r for r in cr.plan_repair(img, venue).revivals if r.client_order_id == PATH_CORPSE)
    fills = [cr.to_dict(e) for e in rv.events if cr.to_dict(e)["type"] == "OrderFilled"]
    assert [f["last_qty"] for f in fills] == ["80", "11", "4", "3", "14"]
    assert rv.kept_fill_qty == 98 and rv.synthetic_fill_qty == 14


def test_a_corpse_the_venue_canceled_without_a_fill_is_left_alone(image, venue):
    """#801 cancelled 8 at the venue on 09-08 (27 such corpses live on instruments outside this fixture):
    REJECTED in the cache, canceled at the venue, nothing moved. Nothing to revive, nothing to rebuild."""
    v = copy.deepcopy(venue)
    real = v.orders[CRAK_CORPSE]
    v.orders[CRAK_CORPSE] = cr.VenueOrder(real.client_order_id, "canceled", Decimal(0), None, None, real.venue_order_id)
    p = cr.plan_repair(image, v)
    assert CRAK_CORPSE in p.left_alone and p.left_alone[CRAK_CORPSE].startswith("venue says canceled")
    assert CRAK_CORPSE not in {r.client_order_id for r in p.revivals}
    assert not any(rw.position_id.startswith("CRAK.") for rw in p.rewrites)


def test_a_corpse_the_venue_does_not_know_is_left_exactly_as_it_is(image, venue):
    """Absence at the venue is not evidence the order rested — it may never have landed. No revival, and
    therefore no position rewrite on that instrument."""
    v = copy.deepcopy(venue)
    del v.orders[PATH_CORPSE]
    p = cr.plan_repair(image, v)
    assert PATH_CORPSE not in {r.client_order_id for r in p.revivals}
    assert not any(rw.position_id.startswith("PATH.XNYS-") for rw in p.rewrites)


def test_a_genuine_venue_rejection_is_not_a_corpse(image, venue):
    img = copy.deepcopy(image)
    events = img.orders[PATH_CORPSE]
    idx = next(i for i, e in enumerate(events) if cr.to_dict(e)["type"] == "OrderRejected")
    d = cr.to_dict(events[idx])
    d["reason"] = "insufficient qty available for order (requested: 112, available: 0)"
    events[idx] = cr.obj_to_bytes(cr.dict_to_obj(d))
    assert PATH_CORPSE not in cr.find_corpses(img, venue)


# --------------------------------------------------------------------------------------------------
# The position plane
# --------------------------------------------------------------------------------------------------


def test_the_lane_whose_stop_fired_holds_exactly_its_entries_after_it(plan, image):
    """PATH: the stop closed TECHIVOL's 112 at 13:35; TECHIVOL bought 126 @ 15.21 at 16:00 (`kumo-8b68…`,
    the venue's own avg 15.21). The transfer legs (TR-…) and repair legs (RPR-…) are not entries."""
    by_pid = {rw.position_id: rw for rw in plan.rewrites}
    path = by_pid[PATH_LANE]
    assert path.before.startswith("SHORT 192") and path.after == "LONG 126 @ 15.2100 (open)"
    fills = [cr.to_dict(e) for e in path.events]
    assert [(f["client_order_id"][:9], f["last_qty"]) for f in fills] == [("kumo-8b68", "126")]
    wday = by_pid["WDAY.XNAS-TECHIVOL-005"]
    assert wday.before.startswith("SHORT 3") and wday.after == "LONG 8 @ 184.9700 (open)"


def test_every_mirrored_short_and_its_minted_counterparty_go(plan):
    gone = {rw.position_id for rw in plan.rewrites if not rw.is_open and not rw.unchanged}
    assert gone == {
        "CRM.XNYS-EXTERNAL", "CRM.XNYS-TECHIVOL-005",
        "HALO.XNAS-EXTERNAL", "HALO.XNAS-BCTROT-004", "HALO.XNAS-MOMENTUM-002",
        "PATH.XNYS-EXTERNAL", "WDAY.XNAS-EXTERNAL",
    }
    for rw in plan.rewrites:
        if rw.position_id in gone:
            assert rw.events == []


def test_a_lane_position_the_venue_backs_is_untouched(plan):
    """LAND: MOMENTUM's corpse fired (217 @ 9.62) but BCTROT's 212 is what the venue holds. Rule 4."""
    land = next(rw for rw in plan.rewrites if rw.position_id == "LAND.XNAS-BCTROT-004")
    assert land.unchanged and land.is_open and land.after.startswith("LONG 212")
    assert not any(rw.position_id.startswith(("CRAK.", "PAGP.")) for rw in plan.rewrites), "no corpse fired there"


def test_the_plan_nets_to_the_venue_on_every_instrument_a_corpse_fired_on(plan, image, venue):
    assert plan.refusals == {}
    checked = set()
    for instrument_id in FIRED:
        instrument = cr.to_obj(image.instruments[instrument_id])
        net = Decimal(0)
        for rw in plan.rewrites:
            if rw.position_id.startswith(instrument_id + "-") and rw.is_open:
                net += _net_of(instrument, rw.events)
        assert net == venue.positions.get(instrument_id.split(".")[0], Decimal(0)), instrument_id
        checked.add(instrument_id)
    assert checked == FIRED  # vacuity guard: thirteen symbols, not "0 of 0"


def test_an_open_short_in_a_lane_whose_stop_did_not_fire_refuses(image, venue):
    """A mint lands in the lane whose order sold, so rule 1 owns every short this incident produced.
    A short anywhere else is a state the rules do not explain — REFUSE, do not delete, do not keep."""
    img = copy.deepcopy(image)
    poisoned = [cr.to_dict(e) for e in img.positions[PATH_LANE]]
    for d in poisoned:
        d["strategy_id"], d["position_id"] = "MOMENTUM-002", "PATH.XNYS-MOMENTUM-002"
    img.positions["PATH.XNYS-MOMENTUM-002"] = [cr.obj_to_bytes(cr.dict_to_obj(d)) for d in poisoned]
    p = cr.plan_repair(img, venue)
    assert "PATH.XNYS-MOMENTUM-002" in p.unexplained and "SHORT 192" in p.unexplained["PATH.XNYS-MOMENTUM-002"]
    with pytest.raises(cr.RepairRefused):
        cr.apply_plan(p, FakeRedis.from_image(img))


def test_a_venue_that_disagrees_refuses_the_whole_plan_and_writes_nothing(image, venue):
    v = copy.deepcopy(venue)
    v.positions["PATH"] = Decimal(127)
    p = cr.plan_repair(image, v)
    assert p.refusals == {"PATH": (Decimal(126), Decimal(127))}
    r = FakeRedis.from_image(image)
    before = r.snapshot()
    with pytest.raises(cr.RepairRefused):
        cr.apply_plan(p, r)
    assert r.snapshot() == before


def _clone_corpse(img, venue, new_coid, *, status, filled_qty, filled_at, avg):
    """A second PATH corpse in the same lane, from the real one's bytes, with the venue's answer given."""
    events = []
    for raw in img.orders[PATH_CORPSE]:
        d = cr.to_dict(raw)
        d["client_order_id"] = new_coid
        events.append(cr.obj_to_bytes(cr.dict_to_obj(d)))
    img.orders[new_coid] = events
    real = venue.orders[PATH_CORPSE]
    # Same venue id as the real corpse's OrderAccepted: Nautilus (and the planner) refuse a fill whose
    # venue id is not the order's own.
    venue.orders[new_coid] = cr.VenueOrder(new_coid, status, Decimal(filled_qty), Decimal(avg), cr._iso_to_ns(filled_at), real.venue_order_id)


def test_a_genuine_external_holding_on_a_fired_instrument_is_refused_not_deleted(image, venue):
    """Rule 3 would delete PATH.XNYS-EXTERNAL either way; what protects a REAL hand-bought 192 is the net
    check: if the venue held 126 + 192, the plan after deletion nets 126 and must refuse."""
    v = copy.deepcopy(venue)
    v.positions["PATH"] = Decimal(126 + 192)
    p = cr.plan_repair(image, v)
    assert p.refusals == {"PATH": (Decimal(126), Decimal(318))}
    with pytest.raises(cr.RepairRefused):
        cr.apply_plan(p, FakeRedis.from_image(image))


def test_a_corpse_the_venue_canceled_after_a_partial_fill_is_revived_CANCELED_with_the_fill(image, venue):
    img, v = copy.deepcopy(image), copy.deepcopy(venue)
    _clone_corpse(img, v, "PROT-SELL-PATH-XNYS-00000000", status="canceled", filled_qty=100, filled_at="2026-09-04T16:30:00Z", avg="15.10")
    v.positions["PATH"] = Decimal(0)  # the 100 sold came out of the 126 bought at 16:00; nothing bought after
    p = cr.plan_repair(img, v)
    rv = next(r for r in p.revivals if r.client_order_id == "PROT-SELL-PATH-XNYS-00000000")
    order = cr.replay_order(rv.events)
    assert order.status_string() == "CANCELED" and str(order.filled_qty) == "100"
    assert p.refusals == {} and p.unexplained == {}
    lane = next(rw for rw in p.rewrites if rw.position_id == PATH_LANE)
    assert not lane.is_open, "no `kumo-` entry after the later stop, so the lane is flat"


def test_two_corpses_in_one_lane_take_the_later_fill_as_the_cut(image, venue):
    """The clone sorts BEFORE the real corpse; a last-write-wins dict would keep 13:35 and rebuild the
    lane from the 16:00 entry the later stop had already sold."""
    img, v = copy.deepcopy(image), copy.deepcopy(venue)
    _clone_corpse(img, v, "PROT-SELL-PATH-XNYS-00000000", status="filled", filled_qty=126, filled_at="2026-09-04T16:30:00Z", avg="15.10")
    v.positions["PATH"] = Decimal(0)
    p = cr.plan_repair(img, v)
    assert p.refusals == {}, p.summary()
    assert not next(rw for rw in p.rewrites if rw.position_id == PATH_LANE).is_open


def test_a_lane_fill_with_no_position_id_books_to_the_netting_id(image, venue):
    img = copy.deepcopy(image)
    events = img.orders["kumo-8b68fc87e100dbcb6e07"]
    d = cr.to_dict(events[-1])
    assert d["type"] == "OrderFilled" and d["position_id"] == PATH_LANE
    d["position_id"] = None
    events[-1] = cr.obj_to_bytes(cr.dict_to_obj(d))
    p = cr.plan_repair(img, venue)
    lane = next(rw for rw in p.rewrites if rw.position_id == PATH_LANE)
    assert lane.after == "LONG 126 @ 15.2100 (open)"
    assert cr.to_dict(lane.events[0])["position_id"] == PATH_LANE


def test_a_trim_after_the_re_entry_is_part_of_what_the_lane_holds(image, venue):
    img, v = copy.deepcopy(image), copy.deepcopy(venue)
    buy = cr.to_dict(img.orders["kumo-8b68fc87e100dbcb6e07"][-1])
    trim = dict(buy, client_order_id="kumo-trim0000000000000000", order_side="SELL", last_qty="26", trade_id="00000000-0000-4000-8000-000000000026",
                ts_event=str(int(buy["ts_event"]) + 60_000_000_000), ts_init=str(int(buy["ts_init"]) + 60_000_000_000))
    lifecycle = []  # Init, Submitted, Accepted, Filled — a real order's shape; a bare [Init, Filled] cannot replay
    for raw in img.orders["kumo-8b68fc87e100dbcb6e07"][:-1]:
        d = dict(cr.to_dict(raw), client_order_id="kumo-trim0000000000000000")
        if d["type"] == "OrderInitialized":
            d.update(order_side="SELL", quantity="26")
        lifecycle.append(cr.obj_to_bytes(cr.dict_to_obj(d)))
    img.orders["kumo-trim0000000000000000"] = lifecycle + [cr.obj_to_bytes(cr.dict_to_obj(trim))]
    v.positions["PATH"] = Decimal(100)
    p = cr.plan_repair(img, v)
    assert p.refusals == {}, p.summary()
    assert next(rw for rw in p.rewrites if rw.position_id == PATH_LANE).after == "LONG 100 @ 15.2100 (open)"


def test_a_venue_answer_about_another_order_id_refuses(image, venue):
    v = copy.deepcopy(venue)
    real = v.orders[PATH_CORPSE]
    v.orders[PATH_CORPSE] = cr.VenueOrder(real.client_order_id, real.status, real.filled_qty, real.filled_avg_price, real.filled_at_ns, "not-the-cached-venue-id")
    with pytest.raises(cr.RepairRefused, match="venue order id"):
        cr.plan_repair(image, v)


def test_a_missing_instrument_refuses_instead_of_crashing(image, venue):
    img = copy.deepcopy(image)
    del img.instruments["PATH.XNYS"]
    with pytest.raises(cr.RepairRefused, match="PATH.XNYS"):
        cr.plan_repair(img, venue)


# --------------------------------------------------------------------------------------------------
# Apply — through a Redis double that answers exactly what production's client answers (bytes)
# --------------------------------------------------------------------------------------------------


class FakeRedis:
    """Only the calls the tool makes; anything else raises. Keys and members come back as BYTES,
    because that is what `redis.Redis()` without `decode_responses` returns and the loader decodes."""

    def __init__(self):
        self.lists: dict[str, list[bytes]] = {}
        self.strings: dict[str, bytes] = {}
        self.sets: dict[str, set[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    @classmethod
    def from_image(cls, image: cr.CacheImage) -> "FakeRedis":
        r = cls()
        P = cr.KEY_PREFIX
        for coid, ev in image.orders.items():
            r.lists[f"{P}:orders:{coid}"] = list(ev)
        for pid, ev in image.positions.items():
            r.lists[f"{P}:positions:{pid}"] = list(ev)
        raw = json.loads((FIXTURES / "redis.json").read_text())
        for pid, ev in raw["snapshots_positions"].items():
            r.lists[f"{P}:snapshots:positions:{pid}"] = [base64.b64decode(x) for x in ev]
        for iid, b in image.instruments.items():
            r.strings[f"{P}:instruments:{iid}"] = b
        for name, members in image.index.items():
            r.sets[f"{P}:index:{name}"] = set(members)
        r.hashes[f"{P}:index:order_position"] = dict(image.order_position)
        return r

    def snapshot(self):
        return copy.deepcopy((self.lists, self.strings, self.sets, self.hashes))

    # reads
    def scan_iter(self, pattern):
        for k in list(self.lists) + list(self.strings):
            if fnmatch.fnmatchcase(k, pattern):
                yield k.encode()

    def lrange(self, key, start, end):
        return list(self.lists.get(self._k(key), []))

    def get(self, key):
        return self.strings.get(self._k(key))

    def smembers(self, key):
        return {m.encode() for m in self.sets.get(self._k(key), set())}

    def hgetall(self, key):
        return {k.encode(): v.encode() for k, v in self.hashes.get(self._k(key), {}).items()}

    # writes (queued through the pipeline)
    def _type_check(self, key, store):
        for name, other in (("lists", self.lists), ("strings", self.strings), ("sets", self.sets), ("hashes", self.hashes)):
            if other is not store and key in other:
                raise TypeError(f"WRONGTYPE {key} is a {name}")  # what redis answers, as an exception

    def delete(self, key):
        self.lists.pop(key, None)
        self.strings.pop(key, None)
        self.sets.pop(key, None)
        self.hashes.pop(key, None)

    def rpush(self, key, *values):
        if not values:
            raise TypeError("wrong number of arguments for 'rpush'")  # redis refuses an empty push
        self._type_check(key, self.lists)
        self.lists.setdefault(key, []).extend(values)

    def sadd(self, key, member):
        self._type_check(key, self.sets)
        self.sets.setdefault(key, set()).add(member)

    def srem(self, key, member):
        self.sets.setdefault(key, set()).discard(member)

    def hset(self, key, field=None, value=None, mapping=None):
        self._type_check(key, self.hashes)
        h = self.hashes.setdefault(key, {})
        if mapping:
            h.update(mapping)
        else:
            h[field] = value

    def hdel(self, key, field):
        self.hashes.get(key, {}).pop(field, None)

    def pipeline(self, transaction=True):
        return _Pipe(self)

    @staticmethod
    def _k(key):
        return key.decode() if isinstance(key, bytes) else key


class _Pipe:
    def __init__(self, r):
        self.r, self.ops = r, []

    def __getattr__(self, name):
        if name not in ("delete", "rpush", "sadd", "srem", "hset", "hdel"):
            raise AttributeError(name)

        def queue(*a, **kw):
            self.ops.append((name, a, kw))

        return queue

    def execute(self):
        for name, a, kw in self.ops:
            getattr(self.r, name)(*a, **kw)
        self.ops = []


def test_apply_then_replan_is_a_noop(image, venue, plan):
    """The whole point of provenance-bearing, deterministic edits: a second run finds nothing."""
    r = FakeRedis.from_image(image)
    n = cr.apply_plan(plan, r, provenance={"test": True})
    assert n == len(plan.revivals) + sum(1 for rw in plan.rewrites if not rw.unchanged)
    again = cr.plan_repair(cr.CacheImage.from_redis(r), venue)
    assert again.is_noop, again.summary()


def test_apply_keeps_the_index_sets_consistent_with_the_objects(image, venue, plan):
    r = FakeRedis.from_image(image)
    cr.apply_plan(plan, r)
    P = cr.KEY_PREFIX
    gone = {rw.position_id for rw in plan.rewrites if not rw.is_open and not rw.unchanged}
    for pid in gone:
        assert f"{P}:positions:{pid}" not in r.lists
        assert f"{P}:snapshots:positions:{pid}" not in r.lists
        for idx in ("positions", "positions_open", "positions_closed"):
            assert pid not in r.sets[f"{P}:index:{idx}"]
        assert pid not in r.hashes[f"{P}:index:order_position"].values()
    assert PATH_LANE in r.sets[f"{P}:index:positions_open"] and PATH_LANE not in r.sets[f"{P}:index:positions_closed"]
    assert CRAK_CORPSE in r.sets[f"{P}:index:orders_open"] and CRAK_CORPSE not in r.sets[f"{P}:index:orders_closed"]
    assert PATH_CORPSE in r.sets[f"{P}:index:orders_closed"] and PATH_CORPSE not in r.sets[f"{P}:index:orders_open"]
    assert r.hashes[f"{P}:index:order_position"][PATH_CORPSE] == PATH_LANE
    assert len(r.hashes["kumo:repair:807"]) == 1  # provenance row


def test_the_rewritten_lists_replay_through_the_installed_classes_to_the_venue(image, venue, plan):
    """Not the planner's own description — the bytes, read back the way the node reads them."""
    r = FakeRedis.from_image(image)
    cr.apply_plan(plan, r)
    after = cr.CacheImage.from_redis(r)
    assert cr.replay_order(after.orders[PATH_CORPSE]).status_string() == "FILLED"
    assert cr.replay_order(after.orders[CRAK_CORPSE]).status_string() == "ACCEPTED"
    instrument = cr.to_obj(after.instruments["PATH.XNYS"])
    assert _net_of(instrument, after.positions[PATH_LANE]) == Decimal(126)
    assert "PATH.XNYS-EXTERNAL" not in after.positions
    assert not any(pid.startswith(("CRM.XNYS-", "HALO.XNAS-")) and pid in after.index["positions_open"] for pid in after.positions)
