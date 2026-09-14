"""Answer, empirically, the two questions that gate #245 / #252 / #169.

Both PEAK and PYRAMID lose a position's protection whenever they change a resting trailing stop. The
cause is not an ordering mistake — it is that Alpaca reserves shares against a resting sell order and
releases them only when a cancel is CONFIRMED, while `_cancel` is fire-and-forget. So cancel-then-submit
gets `available: 0` on the new order, and submit-then-cancel gets the same rejection on the replacement
and then cancels the only protection there was. Neither ordering can work. (FIG, 2026-08-11, #240.)

`PATCH /v2/orders/{id}` is the primitive that should dissolve this: the venue swaps the order atomically,
the old one goes to `replaced`, and no reservation is ever released. Alpaca's docs bless replacing `trail`
on a trailing stop. Two things they do NOT settle, and both change the design:

  Q1. Does the HIGH WATER MARK survive a replace?
      A trailing stop's stop price is derived from the hwm, which "tracks the highest price since the
      order was submitted". A replace returns a NEW order id. If the hwm carries over, tightening the
      trail on a position that has already run up triggers an exit immediately — which is exactly what a
      full exit wants, and would let PEAK exit without a second order at all. If the hwm RESETS to the
      current price, a tightened trail just sits below the market and never elects, and the whole
      fast-path idea is dead.

  Q2. Can `qty` be replaced on a `trailing_stop`?
      The generic replace endpoint exposes `qty`; the trailing-stop section only explicitly blesses
      `trail`. Every trim depends on this. If qty cannot be replaced, trims need a different mechanism
      from tightens and the two paths diverge.

Neither can be answered from the documentation, and guessing here would repeat the mistake that produced
the bug. So: measure.

WHAT THIS DOES TO THE ACCOUNT
    Buys 1 share of a cheap, liquid symbol, rests a sell trailing stop against it, replaces that stop
    twice, then cancels the stop and sells the share back. Paper account only — it refuses to run against
    a live endpoint. Roughly $30 of notional for a couple of minutes, and two round-trip commissions of
    zero.

WHEN TO RUN IT
    During regular trading hours. Trailing stops do not trigger outside RTH and orders submitted after
    the close are queued for the next session, so a run outside RTH measures nothing. The script checks
    the clock and refuses.

USAGE
    export APCA_API_KEY_ID=$(security find-generic-password -s alpaca-key-paper -w)
    export APCA_API_SECRET_KEY=$(security find-generic-password -s alpaca-secret-paper -w)
    python -m scripts.probe_trailing_replace            # dry run: checks access, places nothing
    python -m scripts.probe_trailing_replace --live     # actually trades
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.providers.alpaca.http import AlpacaHttpClient, AlpacaHttpError

PAPER_TRADING = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"

#: Cheap enough that a single share is immaterial, liquid enough to fill instantly and to print often
#: enough that a trailing stop's hwm actually moves during the probe.
SYMBOL = "F"

#: Wide enough that the initial stop cannot elect while we are setting up.
INITIAL_TRAIL_PCT = "10"
#: Tight enough that, IF the hwm carries across a replace, the stop should elect almost immediately.
TIGHT_TRAIL_PCT = "0.05"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def poll_until(http: AlpacaHttpClient, order_id: str, want: set[str], timeout: float = 30.0) -> dict:
    """Alpaca is asynchronous everywhere that matters: an HTTP 200 on submit, cancel or replace says the
    request was accepted, NOT that it took effect. Every step here waits for the order to actually reach a
    state — the discipline whose absence is the bug being investigated."""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = await http._get(http._trading, f"/v2/orders/{order_id}")
        if last.get("status") in want:
            return last
        await asyncio.sleep(0.5)
    raise TimeoutError(f"order {order_id} stuck in {last.get('status')!r}, wanted one of {sorted(want)}")


async def main(live: bool) -> int:
    key, secret = os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("APCA_API_KEY_ID / APCA_API_SECRET_KEY must be in the environment")

    base = os.environ.get("ALPACA_TRADING_BASE", PAPER_TRADING)
    if "paper" not in base:
        raise SystemExit(f"refusing to run against a non-paper endpoint: {base}")

    http = AlpacaHttpClient(key, secret, base, DATA)
    await http.connect()
    try:
        acct = await http.get_account()
        log(f"account {acct.get('account_number')} · status {acct.get('status')} · {base}")

        clock = await http.get_clock()
        if not clock.get("is_open"):
            log(f"market is CLOSED (next open {clock.get('next_open')}).")
            log("Trailing stops do not trigger outside RTH and new orders are queued to the next")
            log("session, so a run now would measure nothing. Re-run during regular hours.")
            return 2

        if not live:
            log("DRY RUN — access and clock verified, nothing placed. Re-run with --live to measure.")
            return 0

        # -- setup: one share to rest a stop against ------------------------------------------------
        log(f"buying 1 {SYMBOL} to have something to protect")
        buy = await http.submit_order(
            {"symbol": SYMBOL, "qty": "1", "side": "buy", "type": "market", "time_in_force": "day"}
        )
        await poll_until(http, buy["id"], {"filled"})
        log("filled")

        stop = await http.submit_order(
            {
                "symbol": SYMBOL,
                "qty": "1",
                "side": "sell",
                "type": "trailing_stop",
                "trail_percent": INITIAL_TRAIL_PCT,
                "time_in_force": "gtc",
            }
        )
        resting = await poll_until(http, stop["id"], {"new"})
        hwm_before, stop_before = resting.get("hwm"), resting.get("stop_price")
        log(f"trailing stop resting · hwm={hwm_before} stop={stop_before} trail={INITIAL_TRAIL_PCT}%")

        # Let the hwm establish itself above the current price, so a reset is DISTINGUISHABLE from a
        # carry-over. Without this the two answers can look identical.
        log("holding 45s to let the hwm move above the entry print…")
        await asyncio.sleep(45)
        resting = await http._get(http._trading, f"/v2/orders/{stop['id']}")
        hwm_before, stop_before = resting.get("hwm"), resting.get("stop_price")
        log(f"before replace · hwm={hwm_before} stop={stop_before}")

        # -- Q1: does the hwm survive a replace? ----------------------------------------------------
        answers: dict[str, Any] = {"hwm_before": hwm_before, "stop_before": stop_before}
        try:
            replaced = await http.replace_order(stop["id"], {"trail": TIGHT_TRAIL_PCT})
            after = await poll_until(http, replaced["id"], {"new", "filled", "accepted", "pending_new"})
            answers["Q1_replace_accepted"] = True
            answers["hwm_after"] = after.get("hwm")
            answers["stop_after"] = after.get("stop_price")
            answers["new_order_id"] = replaced["id"]
            old = await http._get(http._trading, f"/v2/orders/{stop['id']}")
            answers["old_order_status"] = old.get("status")
            answers["Q1_hwm_survived"] = after.get("hwm") == hwm_before
            log(f"after replace · hwm={after.get('hwm')} stop={after.get('stop_price')} "
                f"status={after.get('status')} · old order now {old.get('status')!r}")
        except AlpacaHttpError as exc:
            answers["Q1_replace_accepted"] = False
            answers["Q1_error"] = exc.reason
            log(f"replace of `trail` REJECTED — {exc.reason}")

        # -- Q2: can qty be replaced on a trailing stop? --------------------------------------------
        # Needs 2 shares to be meaningful (1 -> 1 proves nothing), so top up first.
        live_id = answers.get("new_order_id", stop["id"])
        try:
            top_up = await http.submit_order(
                {"symbol": SYMBOL, "qty": "1", "side": "buy", "type": "market", "time_in_force": "day"}
            )
            await poll_until(http, top_up["id"], {"filled"})
            widened = await http.replace_order(live_id, {"qty": "2", "trail": INITIAL_TRAIL_PCT})
            after2 = await poll_until(http, widened["id"], {"new", "accepted", "pending_new"})
            answers["Q2_qty_replace_accepted"] = True
            answers["qty_after"] = after2.get("qty")
            live_id = widened["id"]
            log(f"qty replace accepted · qty={after2.get('qty')}")
        except AlpacaHttpError as exc:
            answers["Q2_qty_replace_accepted"] = False
            answers["Q2_error"] = exc.reason
            log(f"replace of `qty` REJECTED — {exc.reason}")

        # -- teardown -------------------------------------------------------------------------------
        log("cleaning up")
        try:
            await http.cancel_order(live_id)
            await poll_until(http, live_id, {"canceled", "filled", "expired"})
        except (AlpacaHttpError, TimeoutError) as exc:
            log(f"stop already gone or would not cancel: {exc}")
        positions = {p["symbol"]: p for p in (await http.list_positions() or [])}
        held = positions.get(SYMBOL)
        if held:
            qty = abs(int(float(held["qty"])))
            log(f"selling back {qty} {SYMBOL}")
            back = await http.submit_order(
                {"symbol": SYMBOL, "qty": str(qty), "side": "sell", "type": "market", "time_in_force": "day"}
            )
            await poll_until(http, back["id"], {"filled"})

        # -- what it means --------------------------------------------------------------------------
        print("\n" + "=" * 78)
        for k, v in answers.items():
            print(f"  {k:28} {v}")
        print("=" * 78)
        if answers.get("Q1_hwm_survived"):
            print("Q1: hwm SURVIVES a replace. Tightening the trail on a run-up position elects at once,")
            print("    so PEAK can exit without a second order and #245's tighten path is a pure replace.")
        elif answers.get("Q1_replace_accepted"):
            print("Q1: hwm RESETS on replace. The tightened stop sits below the market and will not elect —")
            print("    the replace fast-path is dead for exits; it still works for widening/tightening a")
            print("    trail going forward, but an exit needs the cancel-confirm saga.")
        else:
            print("Q1: `trail` cannot be replaced at all. The whole PATCH direction is dead; #245 and #252")
            print("    both need the broker-action saga.")
        if answers.get("Q2_qty_replace_accepted"):
            print("Q2: qty CAN be replaced on a trailing stop — trims can resize protection atomically.")
        else:
            print("Q2: qty CANNOT be replaced on a trailing stop — trims cannot reuse the tighten path and")
            print("    need their own mechanism.")
        return 0
    finally:
        await http.close()


# ---------------------------------------------------------------------------------------------------
# M1 (#898 / #872 arming): the STOP half of the replace contract, which the run above never measured.
#
# Q3. On a plain STOP, does a qty-only PATCH preserve `stop_price`? The oversize shrink in
#     `exec_client._modify_order` PATCHes `{"qty"}` alone; if the venue does not inherit the trigger, the
#     replace 422s and a 50-share floor keeps resting against 30 held — a 20-share short on trigger.
# Q4. Is `type` patchable — STOP -> TRAILING_STOP, and TRAILING_STOP -> STOP? If yes, the trail -> floor
#     mode switch is a modify-in-place and the naked cancel-then-place window (#898 B2) does not exist
#     on that path. If no, the window is venue-forced and B2 is about its LENGTH.
# Q5. On a TRAILING_STOP, is `stop_price` alone patchable (a second way the switch might be expressible)?
#
# RAW RESPONSES are recorded verbatim — status and body — because the inference is what this replaces.
# Symbol: outside every lane's universe and every held name on the frozen paper stack (checked against
# /pool and /positions on 2026-09-11 before the run). Two shares, cleaned up in the same run.
# ---------------------------------------------------------------------------------------------------

M1_SYMBOL = "BIL"


def _raw(exc: AlpacaHttpError) -> dict:
    return {"status": exc.status, "body": exc.body}


async def _order(http: AlpacaHttpClient, order_id: str) -> dict:
    return await http._get(http._trading, f"/v2/orders/{order_id}")


async def main_m1(live: bool) -> int:
    key, secret = os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("APCA_API_KEY_ID / APCA_API_SECRET_KEY must be in the environment")
    base = os.environ.get("ALPACA_TRADING_BASE", PAPER_TRADING)
    if "paper" not in base:
        raise SystemExit(f"refusing to run against a non-paper endpoint: {base}")

    http = AlpacaHttpClient(key, secret, base, DATA)
    await http.connect()
    raw: dict[str, Any] = {}
    try:
        acct = await http.get_account()
        log(f"account {acct.get('account_number')} · status {acct.get('status')} · {base}")
        clock = await http.get_clock()
        if not clock.get("is_open"):
            log(f"market is CLOSED (next open {clock.get('next_open')}); a replace outside RTH measures nothing.")
            return 2
        if not live:
            log("DRY RUN — access and clock verified, nothing placed. Re-run with --m1 --live to measure.")
            return 0
        if any(p["symbol"] == M1_SYMBOL for p in (await http.list_positions() or [])):
            raise SystemExit(f"{M1_SYMBOL} is already held on this account — pick another symbol")

        # -- setup: two shares, one resting STOP for both ---------------------------------------------
        buy = await http.submit_order(
            {"symbol": M1_SYMBOL, "qty": "2", "side": "buy", "type": "market", "time_in_force": "day"})
        filled = await poll_until(http, buy["id"], {"filled"})
        px = float(filled["filled_avg_price"])
        floor = f"{px * 0.97:.2f}"
        log(f"bought 2 {M1_SYMBOL} @ {px} · floor for the probe = {floor}")

        stop = await http.submit_order(
            {"symbol": M1_SYMBOL, "qty": "2", "side": "sell", "type": "stop", "stop_price": floor,
             "time_in_force": "gtc"})
        resting = await poll_until(http, stop["id"], {"new", "accepted"})
        raw["stop_resting"] = {k: resting.get(k) for k in ("id", "type", "qty", "stop_price", "status")}
        log(f"STOP resting · {raw['stop_resting']}")
        live_id = stop["id"]

        # -- Q3: qty-only PATCH on a STOP -----------------------------------------------------------
        try:
            rep = await http.replace_order(live_id, {"qty": "1"})
            after = await poll_until(http, rep["id"], {"new", "accepted", "pending_new"})
            old = await _order(http, live_id)
            raw["Q3_qty_only_patch"] = {
                "accepted": True, "new": {k: after.get(k) for k in ("id", "type", "qty", "stop_price", "status")},
                "old_status": old.get("status"),
            }
            live_id = rep["id"]
        except AlpacaHttpError as exc:
            raw["Q3_qty_only_patch"] = {"accepted": False, **_raw(exc)}
        log(f"Q3 · {raw['Q3_qty_only_patch']}")

        # -- Q4a: type STOP -> TRAILING_STOP ---------------------------------------------------------
        try:
            rep = await http.replace_order(live_id, {"type": "trailing_stop", "trail_percent": "5"})
            after = await poll_until(http, rep["id"], {"new", "accepted", "pending_new"})
            raw["Q4a_stop_to_trailing"] = {
                "accepted": True,
                "new": {k: after.get(k) for k in ("id", "type", "qty", "stop_price", "trail_percent", "hwm", "status")},
            }
            live_id = rep["id"]
        except AlpacaHttpError as exc:
            raw["Q4a_stop_to_trailing"] = {"accepted": False, **_raw(exc)}
        log(f"Q4a · {raw['Q4a_stop_to_trailing']}")

        # -- swap to a resting TRAILING_STOP for the reverse direction --------------------------------
        await http.cancel_order(live_id)
        await poll_until(http, live_id, {"canceled", "filled", "expired"})
        trail = await http.submit_order(
            {"symbol": M1_SYMBOL, "qty": "1", "side": "sell", "type": "trailing_stop", "trail_percent": "10",
             "time_in_force": "gtc"})
        resting = await poll_until(http, trail["id"], {"new", "accepted"})
        raw["trailing_resting"] = {k: resting.get(k) for k in ("id", "type", "qty", "stop_price", "trail_percent", "hwm", "status")}
        log(f"TRAILING_STOP resting · {raw['trailing_resting']}")
        live_id = trail["id"]

        # -- Q4b: type TRAILING_STOP -> STOP (the direction #872 needs) --------------------------------
        try:
            rep = await http.replace_order(live_id, {"type": "stop", "stop_price": floor})
            after = await poll_until(http, rep["id"], {"new", "accepted", "pending_new"})
            raw["Q4b_trailing_to_stop"] = {
                "accepted": True,
                "new": {k: after.get(k) for k in ("id", "type", "qty", "stop_price", "trail_percent", "status")},
            }
            live_id = rep["id"]
        except AlpacaHttpError as exc:
            raw["Q4b_trailing_to_stop"] = {"accepted": False, **_raw(exc)}
        log(f"Q4b · {raw['Q4b_trailing_to_stop']}")

        # -- Q5: stop_price alone on a TRAILING_STOP --------------------------------------------------
        if raw["Q4b_trailing_to_stop"].get("accepted") is False:
            try:
                rep = await http.replace_order(live_id, {"stop_price": floor})
                after = await poll_until(http, rep["id"], {"new", "accepted", "pending_new"})
                raw["Q5_stop_price_on_trailing"] = {
                    "accepted": True,
                    "new": {k: after.get(k) for k in ("id", "type", "qty", "stop_price", "trail_percent", "status")},
                }
                live_id = rep["id"]
            except AlpacaHttpError as exc:
                raw["Q5_stop_price_on_trailing"] = {"accepted": False, **_raw(exc)}
            log(f"Q5 · {raw['Q5_stop_price_on_trailing']}")
    finally:
        # -- teardown, unconditional: no resting order, no share left for tomorrow's readback ----------
        try:
            log("cleaning up")
            for o in await http.list_orders(status="open") or []:
                if o.get("symbol") == M1_SYMBOL:
                    try:
                        await http.cancel_order(o["id"])
                        await poll_until(http, o["id"], {"canceled", "filled", "expired"})
                    except (AlpacaHttpError, TimeoutError) as exc:
                        log(f"cancel {o['id']}: {exc}")
            held = {p["symbol"]: p for p in (await http.list_positions() or [])}.get(M1_SYMBOL)
            if held:
                qty = abs(int(float(held["qty"])))
                back = await http.submit_order(
                    {"symbol": M1_SYMBOL, "qty": str(qty), "side": "sell", "type": "market", "time_in_force": "day"})
                await poll_until(http, back["id"], {"filled"})
                log(f"sold back {qty} {M1_SYMBOL}")
            left_pos = [p for p in (await http.list_positions() or []) if p["symbol"] == M1_SYMBOL]
            left_ord = [o for o in (await http.list_orders(status="open") or []) if o.get("symbol") == M1_SYMBOL]
            raw["teardown"] = {"positions_left": len(left_pos), "open_orders_left": len(left_ord)}
            log(f"teardown · {raw['teardown']}")
        finally:
            await http.close()
    import json
    print("\n" + "=" * 78 + "\nRAW\n" + json.dumps(raw, indent=2, default=str) + "\n" + "=" * 78)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="actually place orders (paper account only)")
    ap.add_argument("--m1", action="store_true", help="run the #898 M1 STOP-replace probe instead of the trailing-stop one")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main_m1(args.live) if args.m1 else main(args.live)))
