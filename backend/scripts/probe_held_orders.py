"""What does Alpaca's `held` order status actually mean for a protective stop? (#387)

WHY THIS EXISTS
---------------
APA's bracket stop sits at the broker as `status=held`. `list_orders(status="open")` does not return it,
so the cockpit's protection check concludes the position is unprotected and warns every 60 seconds about
$10,320 of exposure.

The severity of that depends entirely on a fact about Alpaca that we do not know: **does a `held` stop
trigger?** If the broker releases it when the shares free up, the position is protected and #387 is a
display bug. If `held` means dormant, the position is genuinely naked and the fix is very different.

This repo does not reason about venue semantics — it measures them. `probe_trailing_replace.py` exists
because guessing at Alpaca produced #245. This is the same move.

READ-ONLY BY DEFAULT. It mines the account's real order history for orders that were ever `held` and
reports what became of them. It places nothing: the paper account carries the operator's live book, and adding
test positions to answer a question is not a trade anyone asked for.

    python scripts/probe_held_orders.py            # read-only history mining
    python scripts/probe_held_orders.py --explain  # also show the open/all filter divergence
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import urllib.request


def _get(path: str) -> list[dict]:
    key, sec = os.environ["APCA_API_KEY_ID"], os.environ["APCA_API_SECRET_KEY"]
    req = urllib.request.Request(
        "https://paper-api.alpaca.markets" + path,
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec},
    )
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


PROTECTIVE = {"stop", "stop_limit", "trailing_stop"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--explain", action="store_true")
    args = ap.parse_args()

    all_orders = _get("/v2/orders?status=all&limit=500&direction=desc")
    open_orders = _get("/v2/orders?status=open&limit=500")
    open_ids = {o["id"] for o in open_orders}

    # 1 — THE FILTER DIVERGENCE, stated as a number rather than asserted.
    held = [o for o in all_orders if o["status"] == "held"]
    held_protective = [o for o in held if o["type"] in PROTECTIVE and o["side"] == "sell"]
    invisible = [o for o in held_protective if o["id"] not in open_ids]
    print(f"orders (all)            {len(all_orders)}")
    print(f"orders (open filter)    {len(open_orders)}")
    print(f"status=held             {len(held)}   protective sells among them: {len(held_protective)}")
    print(f"HELD PROTECTIVE STOPS INVISIBLE TO status=open: {len(invisible)}")
    for o in invisible:
        print(f"   {o['symbol']:<7} {o['type']:<13} qty={o['qty']:<6} stop={o.get('stop_price')}")

    # 2 — WHAT BECOMES OF A HELD ORDER. The question the fix depends on.
    print("\nterminal states of every protective sell in the record:")
    by_state = collections.Counter(
        o["status"] for o in all_orders if o["type"] in PROTECTIVE and o["side"] == "sell"
    )
    for state, n in sorted(by_state.items(), key=lambda kv: -kv[1]):
        print(f"   {state:<14} {n}")

    # NAMED FOR WHAT IT ACTUALLY MEASURES (review finding 8). This was called `filled_from_held` and
    # printed as "a stop that filled proves the venue does release them" — but it is computed from
    # CURRENT status alone and has no linkage to `held` whatsoever. Alpaca's `/v2/orders` returns the
    # current status of an order and nothing about the states it passed through, so no read of this
    # endpoint can show that a given filled stop was ever `held`. The old headline was an unsupported
    # claim standing where the severity of #387 was being decided.
    filled_protective = [
        o for o in all_orders
        if o["type"] in PROTECTIVE and o["side"] == "sell" and o["status"] == "filled"
    ]
    print(f"\nprotective sells CURRENTLY in FILLED: {len(filled_protective)}")
    print("   NOT the same as 'filled from held'. /v2/orders reports current status only — it carries no")
    print("   state history, so this number says nothing about whether any of them was ever `held`.")
    print("   Correlating the two needs /v2/account/activities, whose FILL rows carry `order_id`, matched")
    print("   against held ids recorded by an EARLIER run of this probe.")

    if args.explain:
        print("\nAPA, every state:")
        for o in [x for x in all_orders if x["symbol"] == "APA"]:
            print(f"   {o['side']:<5} {o['type']:<13} status={o['status']:<11} "
                  f"stop={o.get('stop_price')} limit={o.get('limit_price')} qty={o['qty']}")

    print("\nNOT ANSWERED BY THIS RUN: whether a stop sitting in `held` fires when the price is reached.")
    print("That needs a stop to actually trigger while held — observation, not inference.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
