"""Drop the phantom pre-window leg Nautilus minted at boot (#909) — planner tests, red before the planner exists.

THE DEFECT, MEASURED ON ibkr-paper 2026-09-10 20:16:37Z. Nautilus 1.229's `_adjust_mass_status_fills` →
`adjust_fills_for_partial_window` takes the venue's fills in the lookback window (IB: since midnight —
TECHIVOL-005's BUY 39 @ 400.43) and the venue position (62 @ 410.00) and synthesises the fill that must
have happened before the window: BUY 23 at (62×410.00 − 39×400.43)/23 = 426.23, `reconciliation=True`,
`S-` venue and trade ids, ts_event = the report's ts_last. It never consults the cache's own positions
(BCTROT-004's 23 from the 09-09 transfer). The synthetic fill reconciled as an external order, reopened
`GLD.ARCX-EXTERNAL` at 23, cache net went 85 against the venue's 62, the netting check gave up after three
attempts, `book_truth` marked GLD abandoned, and protection — which refuses an instrument whose net
disagrees — left TECHIVOL-005's 39 shares with no stop. A restart created an unprotected position.

THE REPAIR is narrow and refuses rather than guesses: the target leg's CURRENT instance must be exactly one
`reconciliation=True` fill carrying `S-` ids, and the cache net minus that leg must equal the venue's net.
Then the leg is rewritten to its real closed history — the fills the cache's own orders hold for that
position id (09-02 buy 23 @ 426.17, 09-09 transfer sell 23) — so `snapshots:` and realized P&L are
untouched, and the synthetic order is dropped. Deleting instead would also reach net 62 but would lose
the closed history; rewriting keeps it and is what the position looked like before the boot.

The fixture is the real bytes (`fixtures/repair_909`), so a double cannot drift from what Nautilus wrote.
"""

from __future__ import annotations

import base64
import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from api import cache_repair as cr
from api.test_cache_repair import FakeRedis

# READS A PRODUCTION CAPTURE (#1045): skipped by name where api/fixtures/repair_909/ is absent (public tree).
pytestmark = pytest.mark.captured_fixture("repair_909")

FIXTURES = Path(__file__).parent / "fixtures" / "repair_909"
LEG = "GLD.ARCX-EXTERNAL"
MINT_COID = "068dca2b-6c8d-4234-bf00-5fb3a138f1db"


def _image() -> cr.CacheImage:
    return cr.CacheImage.from_fixture(FIXTURES / "redis.json")


def _venue(gld: str = "62") -> cr.VenueTruth:
    return cr.VenueTruth.from_alpaca({}, [{"symbol": "GLD", "qty": gld, "side": "long"}])


def _redis(image: cr.CacheImage) -> FakeRedis:
    r = FakeRedis()
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


def _current_instance(image, pid):
    instrument = cr.to_obj(image.instruments[pid.split("-")[0]])
    return cr.replay_position(instrument, [cr.to_obj(e) for e in image.positions[pid]])[-1]


# -- fixture property first: the bug is reachable ------------------------------------------------------

def test_fixture_property_the_leg_is_one_synthetic_fill_and_the_net_is_off_by_exactly_that_leg():
    image = _image()
    events = [cr.to_dict(e) for e in image.positions[LEG]]
    assert len(events) == 1
    e = events[0]
    assert e["reconciliation"] is True and e["strategy_id"] == "EXTERNAL"
    assert e["venue_order_id"].startswith("S-") and e["trade_id"].startswith("S-")
    assert (e["order_side"], e["last_qty"], e["last_px"]) == ("BUY", "23", "426.23")
    assert e["client_order_id"] == MINT_COID and MINT_COID in image.orders
    net = sum(Decimal(str(_current_instance(image, pid).signed_qty))
              for pid in image.positions if pid in image.index["positions_open"])
    assert net == Decimal(85) and _venue().positions["GLD"] == Decimal(62)
    assert Decimal("426.23") == round((62 * Decimal("410.00") - 39 * Decimal("400.43")) / 23, 2), \
        "the price is derived, not traded — that arithmetic is the proof"
    # the real closed history exists in the cache's own orders, keyed to this position id
    real = [c for c, p in image.order_position.items() if p == LEG and c != MINT_COID]
    assert sorted(real) == ["GLD.ARCX", "TR-15d2f82aee81cfbf4111-S"]


# -- the plan ------------------------------------------------------------------------------------------

def test_the_plan_rewrites_ONLY_the_synthetic_leg_to_its_real_closed_history_and_drops_the_mint_order():
    plan = cr.plan_drop_synthetic_leg(_image(), _venue(), position_id=LEG)
    assert plan.refusals == {} and plan.unexplained == {}
    changed = [rw for rw in plan.rewrites if not rw.unchanged]
    assert [rw.position_id for rw in changed] == [LEG]
    rw = changed[0]
    assert rw.is_open is False
    fills = [cr.to_dict(e) for e in rw.events]
    assert [(f["order_side"], f["last_qty"]) for f in fills] == [("BUY", "23"), ("SELL", "23.0")]
    assert all(not str(f.get("venue_order_id")).startswith("S-") for f in fills), \
        "the rewrite carries only fills the venue reported (the 09-02 one is reconciliation=True with a REAL venue id)"
    assert plan.drop_orders == [MINT_COID]
    assert "synthetic" in plan.summary() and LEG in plan.summary()
    assert plan.is_noop is False


def test_the_plan_refuses_a_leg_that_is_not_synthetic():
    image = _image()
    d = cr.to_dict(image.positions[LEG][0]); d["reconciliation"] = False
    image.positions[LEG] = [cr.obj_to_bytes(cr.dict_to_obj(d))]
    plan = cr.plan_drop_synthetic_leg(image, _venue(), position_id=LEG)
    assert LEG in plan.unexplained and "reconciliation" in plan.unexplained[LEG]
    assert not [rw for rw in plan.rewrites if not rw.unchanged] and plan.drop_orders == []


def test_the_plan_refuses_when_removing_the_leg_does_NOT_reach_the_venue_net():
    plan = cr.plan_drop_synthetic_leg(_image(), _venue("70"), position_id=LEG)
    assert plan.refusals == {"GLD": (Decimal(62), Decimal(70))}
    with pytest.raises(cr.RepairRefused):
        cr.apply_plan(plan, _redis(_image()))


def test_the_plan_refuses_a_leg_with_more_than_one_fill_in_its_current_instance():
    image = _image()
    d = cr.to_dict(image.positions[LEG][0]); d["trade_id"] = "S-second"; d["event_id"] = "11111111-1111-4111-8111-111111111111"
    image.positions[LEG] = image.positions[LEG] + [cr.obj_to_bytes(cr.dict_to_obj(d))]
    plan = cr.plan_drop_synthetic_leg(image, _venue("39"), position_id=LEG)
    assert LEG in plan.unexplained and "one" in plan.unexplained[LEG]


def test_the_plan_refuses_an_unknown_position_id():
    with pytest.raises(cr.RepairRefused, match="not in the cache"):
        cr.plan_drop_synthetic_leg(_image(), _venue(), position_id="GLD.ARCX-NOBODY")


def test_a_leg_whose_lane_is_not_EXTERNAL_is_still_a_synthetic_leg():
    """Paper's instance landed in MOMENTUM-002 (external_order_claims route the mint to the claimant).
    The predicate is the fill's provenance, not the strategy name."""
    image = _image()
    d = cr.to_dict(image.positions[LEG][0]); d["strategy_id"] = "MOMENTUM-002"; d["position_id"] = "GLD.ARCX-MOMENTUM-002"
    image.positions["GLD.ARCX-MOMENTUM-002"] = [cr.obj_to_bytes(cr.dict_to_obj(d))]
    image.index["positions_open"].add("GLD.ARCX-MOMENTUM-002"); image.index["positions"].add("GLD.ARCX-MOMENTUM-002")
    del image.positions[LEG]; image.index["positions_open"].discard(LEG)
    image.order_position[MINT_COID] = "GLD.ARCX-MOMENTUM-002"
    plan = cr.plan_drop_synthetic_leg(image, _venue(), position_id="GLD.ARCX-MOMENTUM-002")
    assert plan.unexplained == {} and plan.refusals == {}
    assert [rw.position_id for rw in plan.rewrites if not rw.unchanged] == ["GLD.ARCX-MOMENTUM-002"]


def test_TWO_synthetic_orders_on_one_position_is_a_class_not_a_corpse_and_is_refused():
    image = _image()
    mint = cr.to_dict(image.orders[MINT_COID][-1])
    second = dict(mint, client_order_id="second-mint", venue_order_id="S-second-v", trade_id="S-second-t",
                  event_id="22222222-2222-4222-8222-222222222222")
    image.orders["second-mint"] = [cr.obj_to_bytes(cr.dict_to_obj(second))]
    image.order_position["second-mint"] = LEG
    plan = cr.plan_drop_synthetic_leg(image, _venue(), position_id=LEG)
    assert LEG in plan.unexplained and "second-mint" in plan.unexplained[LEG] and "two mints" in plan.unexplained[LEG]
    assert plan.drop_orders == [] and not [rw for rw in plan.rewrites if not rw.unchanged]


def test_a_second_synthetic_order_that_NEVER_FILLED_is_still_two_mints():
    """The signature is the S- venue id on the ORDER, not on a fill: an accepted-but-unfilled mint has none."""
    image = _image()
    init = cr.to_dict(image.orders[MINT_COID][0])
    accepted = {"type": "OrderAccepted", "trader_id": init["trader_id"], "strategy_id": init["strategy_id"],
                "instrument_id": init["instrument_id"], "client_order_id": "unfilled-mint",
                "venue_order_id": "S-unfilled-v", "account_id": "INTERACTIVE_BROKERS-DUPTEST02",
                "event_id": "33333333-3333-4333-8333-333333333333", "ts_event": init["ts_init"], "ts_init": init["ts_init"],
                "reconciliation": True}
    image.orders["unfilled-mint"] = [image.orders[MINT_COID][0], cr.obj_to_bytes(cr.dict_to_obj(accepted))]
    image.order_position["unfilled-mint"] = LEG
    plan = cr.plan_drop_synthetic_leg(image, _venue(), position_id=LEG)
    assert LEG in plan.unexplained and "unfilled-mint" in plan.unexplained[LEG]


# -- apply: blast radius is the claim worth measuring ---------------------------------------------------

def test_apply_touches_the_leg_and_the_mint_order_and_NOTHING_else():
    image = _image(); r = _redis(image); P = cr.KEY_PREFIX
    before = r.snapshot()
    plan = cr.plan_drop_synthetic_leg(image, _venue(), position_id=LEG)
    n = cr.apply_plan(plan, r, provenance={"test": True})
    assert n == 2
    lists, strings, sets, hashes = r.lists, r.strings, r.sets, r.hashes
    # the leg: two real fills, closed
    assert len(lists[f"{P}:positions:{LEG}"]) == 2
    assert LEG not in sets[f"{P}:index:positions_open"] and LEG in sets[f"{P}:index:positions_closed"]
    assert LEG in sets[f"{P}:index:positions"]
    # snapshots (the closed history) untouched
    assert lists[f"{P}:snapshots:positions:{LEG}"] == before[0][f"{P}:snapshots:positions:{LEG}"]
    # the mint order is gone from the key and from every index
    assert f"{P}:orders:{MINT_COID}" not in lists
    for idx in ("orders", "orders_open", "orders_closed"):
        assert MINT_COID not in sets[f"{P}:index:{idx}"]
    assert sets[f"{P}:index:orders_open"] == before[2][f"{P}:index:orders_open"], "the resting stop stays open; nothing else moves"
    assert MINT_COID not in hashes[f"{P}:index:order_position"]
    # every other key is byte-identical
    for store, snap in ((lists, before[0]), (strings, before[1]), (sets, before[2]), (hashes, before[3])):
        for k, v in snap.items():
            if LEG in k or MINT_COID in k or k.endswith(":index:positions_open") or k.endswith(":index:positions_closed") \
                    or k.endswith(":index:orders") or k.endswith(":index:orders_closed") or k.endswith(":index:order_position"):
                continue
            assert store.get(k) == v, f"{k} changed"
    # provenance recorded
    assert any("synthetic" in json.dumps(v) for v in hashes.get("kumo:repair:807", {}).values())


def test_after_apply_a_second_plan_is_a_noop_and_the_cache_net_equals_the_venue():
    image = _image(); r = _redis(image)
    cr.apply_plan(cr.plan_drop_synthetic_leg(image, _venue(), position_id=LEG), r)
    after = cr.CacheImage.from_redis(r)
    net = sum(Decimal(str(_current_instance(after, pid).signed_qty)) for pid in after.index["positions_open"])
    assert net == Decimal(62)
    with pytest.raises(cr.RepairRefused, match="not synthetic|not open|reconciliation"):
        # the leg is closed now: asking again must refuse, never re-plan
        cr.plan_drop_synthetic_leg(after, _venue(), position_id=LEG)


# -- the script, end to end on the double ---------------------------------------------------------------

def _run_script(monkeypatch, argv, r):
    import scripts.repair_cache_807 as tool
    monkeypatch.setattr(tool.redis, "Redis", lambda host, port: r)
    monkeypatch.setattr(tool, "engine_heartbeat_age_s", lambda r_: None)
    monkeypatch.setattr(tool, "clients_still_writing", lambda r_: [])
    return tool.main(argv)


def _fake_with_ping(image):
    r = _redis(image)
    r.ping = lambda: True
    r.dump = lambda key: b"DUMP:" + (key if isinstance(key, bytes) else key.encode())  # what backup() reads
    return r


def test_the_script_dry_runs_the_909_mode_and_writes_nothing(monkeypatch, capsys):
    r = _fake_with_ping(_image()); before = r.snapshot()
    rc = _run_script(monkeypatch, ["--venue-json", str(FIXTURES / "venue.json"), "--drop-synthetic-leg", LEG], r)
    out = capsys.readouterr().out
    assert rc == 0 and "dry run" in out and "DROP" in out and LEG in out
    assert r.snapshot() == before


def test_the_script_applies_the_909_mode_only_with_engine_stopped_and_verifies_by_refusal(monkeypatch, tmp_path, capsys):
    r = _fake_with_ping(_image())
    rc = _run_script(monkeypatch, ["--venue-json", str(FIXTURES / "venue.json"), "--drop-synthetic-leg", LEG, "--apply"], r)
    assert rc == 3, "no --engine-stopped → refused"
    rc = _run_script(monkeypatch, ["--venue-json", str(FIXTURES / "venue.json"), "--drop-synthetic-leg", LEG,
                                   "--apply", "--engine-stopped", "--backup", str(tmp_path / "b.json")], r)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "verified: a second plan refuses" in out
    assert (tmp_path / "b.json").exists()
    assert MINT_COID in (tmp_path / "b.json").read_text(), "the dropped order is in the backup"
    assert LEG not in r.sets[f"{cr.KEY_PREFIX}:index:positions_open"]
