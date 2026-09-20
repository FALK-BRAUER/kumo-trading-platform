# UAT — order flows (market CLOSED), 2026-07-28

Scenarios runnable with the US market shut. Every case here either rests an order, exercises client-side
validation, or tests durability — none need a fill. Fill-dependent cases (partial/full sell against a live
position) are in the last section, gated on the 21:30 SGT open.

**Stack under test:** paper. `[execution].provider = alpaca`, `trading_base_url = https://paper-api.alpaca.markets`,
keys from keychain `alpaca-paper-key` (`PK…`). Equity ~$99.5k, cash ~$76.8k — simulated money.

**Two order tickets exist — test both:**
- **Strategy ticket (#51)** — reached via **BUY/SELL** on the symbol detail surface. Strategy-first, MANUAL only
  (MOMENTUM/ETF_AUTO shown disabled). Assisted: entry mechanism + stop mechanism + risk % prefill levels/size.
- **Vanilla ticket (`order.vanilla`)** — the raw broker ticket: side · market/limit/stop · qty · price/trigger ·
  TIF (`day`/`gtc`/`opg`/`cls`) · optional bracket. No prefill; you type it.

**Verification endpoints** (I watch these on every submit):
```
curl -s localhost:8000/orders            # blotter: status, filled_qty, leaves_qty
curl -s localhost:8000/positions         # net position + strategy_id attribution
curl -s localhost:8000/trades            # trade cycles: state, cycle_id
curl -s localhost:8000/external-activity # quarantine plane
curl -s localhost:8000/health            # reconcile_drift must stay []
docker logs -f kumo-paper-engine-1       # what the engine actually did
```

---

## A. Closed-market guard (#113)

| # | Steps | Expected | Pass |
|---|---|---|---|
| A1 | Vanilla ticket → side BUY, type **MARKET**, qty 1, TIF **day** | Submit **blocked**. `marketOrderWouldBeCanceled` fires — a market order can't rest and the venue would kill it in seconds | ☐ |
| A2 | Same, switch TIF to **opg** | Submit **enabled** — opening-auction order is the legitimate escape hatch | ☐ |
| A3 | Same, TIF **cls** | Submit **enabled** | ☐ |
| A4 | Switch type to **LIMIT**, TIF back to `day` | Submit **enabled** — limit orders rest fine, never blocked | ☐ |
| A5 | Type **STOP**, TIF `day` | Submit **enabled** | ☐ |

Known limitation worth confirming, not fixing: `isUsMarketOpen` is **holiday-unaware**. On a market holiday
A1 would wrongly allow a doomed market order. Follow-up = Alpaca `/v2/clock`.

## B. Resting orders — lifecycle

Use a liquid watchlist name (AAPL / SMH / NOW). Price the limit **far from market** so nothing fills at the open.

| # | Steps | Expected | Pass |
|---|---|---|---|
| B1 | LIMIT BUY, qty 5, price ~20% **below** last, TIF `gtc` → submit | Slide-to-confirm required; then row appears in blotter, status working, `leaves_qty=5` | ☐ |
| B2 | Read `/orders` | `client_order_id` present, `venue_order_id` populated (broker accepted it) | ☐ |
| B3 | **Modify** the resting order — change price | Blotter reflects new price, same `client_order_id`, still working | ☐ |
| B4 | **Modify** qty 5 → 8 | `leaves_qty=8`, no duplicate row | ☐ |
| B5 | **Cancel** it | Terminal state renders; `leaves_qty=0`; row does not resurrect on refresh | ☐ |
| B6 | Refresh browser after cancel | Blotter state identical (server is render-truth, not local state) | ☐ |

## C. Bracket geometry (client-side, no broker needed)

Bracket = entry + protective stop + target. `bracketGeoOk` enforces ordering; stop entry is coerced off when
BRACKET is enabled.

| # | Steps | Expected | Pass |
|---|---|---|---|
| C1 | BUY bracket, entry limit 100, stop **105**, target 110 | **Rejected** — stop above entry on a long is not protective | ☐ |
| C2 | BUY bracket, entry 100, stop 95, target **90** | **Rejected** — target below entry on a long | ☐ |
| C3 | BUY bracket, entry 100, stop 95, target 110 | Accepted, submits 3 linked legs | ☐ |
| C4 | Enable BRACKET while type = **stop** | Type coerced to market/limit automatically | ☐ |
| C5 | SELL bracket, entry 100, stop 95 | **Rejected** — protective side inverts for a short | ☐ |
| C6 | After C3, read `/orders` | All three legs present and linked; cancelling the entry cancels the children | ☐ |

## D. Safety edges

| # | Steps | Expected | Pass |
|---|---|---|---|
| D1 | **Slide-to-confirm (#108)** — tap submit without completing the slide | Nothing submits. No order in `/orders` | ☐ |
| D2 | Start the slide, release at ~50% | Slider springs back, no submit | ☐ |
| D3 | **Idempotency (#78)** — resubmit the same `command_id` twice (double-tap submit, or replay the POST) | Exactly **one** order. Ledger dedups on `command_id`/`client_order_id` | ☐ |
| D4 | **Oversell** — SELL qty 10,000 of a name you hold 5 of, as a resting limit | Rejection **surfaced in the UI**, not silently swallowed. `/orders` shows the reject reason | ☐ |
| D5 | SELL a name with **zero** position | Same — visible rejection | ☐ |
| D6 | Qty 0, qty negative, qty non-numeric | Submit disabled, no request fired | ☐ |
| D7 | LIMIT with empty price | Submit disabled (`priceOk` false) | ☐ |

D3 is the highest-value case here — a double-fill is the one bug class that costs real money once this goes live.

## E. Durability — the failure mode today's outage exposed

| # | Steps | Expected | Pass |
|---|---|---|---|
| E1 | Leave a resting order working → `docker restart kumo-paper-engine-1` | Order survives; blotter recovers it; `reconcile_drift` returns `[]` | ☐ |
| E2 | Resting order working → `docker restart kumo-paper-api-1` | Blotter recovers from broker/cache, not from browser state | ☐ |
| E3 | Add + remove a watchlist symbol → restart api | Watchlist identical (Postgres round-trip, not memory) | ☐ |
| E4 | `docker stop kumo-paper-postgres-1` → restart api → watch logs | 10s retry loop, `restarts=0`, self-heals when postgres returns (**verified today**) | ☑ |
| E5 | Restart engine while holding a position | `cycle_id` **survives** — same id, not reminted (#74b.2 durable envelope) | ☐ |

## F. Unclaimed / quarantine (#79)

Current book: **CVS.XNYS 168** and **FIG.XNYS 190**, both `strategy_id=EXTERNAL`, `origin=RECONCILIATION`,
`client_order_id=null`, `status=QUARANTINED`.

| # | Steps | Expected | Pass |
|---|---|---|---|
| F1 | Read `/external-activity` | Both rows QUARANTINED with origin RECONCILIATION | ☑ |
| F2 | Confirm no claim affordance exists in the UI | Correct as designed — #79 is **detect-only**; claim/import policy belongs to the coordinator (**#80**, unbuilt) | ☐ |
| F3 | Place a resting SELL against CVS | Order accepted against the broker net; the position stays unclaimed (selling ≠ claiming) | ☐ |
| F4 | Check the Managed Portfolio tile | Unclaimed rows counted in "held" and in % deployed — the honest number | ☐ |

**What claiming will have to do (#80):** assign a `strategy_id` and mint a durable `cycle_id` so the P&L joins
that strategy's sleeve. Per ADR 0001 the broker net is the only hard anchor — the per-strategy split is
unverifiable by the broker, so a claim is a **human assertion**, and the UI must present it as such.

## G. Known-broken — expect red, don't debug

| # | Case | Status |
|---|---|---|
| G1 | 1h chart: 79/283 bars flat at 108.09 during closed hours | Root-caused 2026-07-28 — `time_bars_build_with_no_updates` at Nautilus default `True`. Unfixed |
| G2 | `CVS` chip overlaps the `PAPER` badge ("CVSPER") | Unfixed |
| G3 | Search box keeps stale text ("sap") after selecting a different symbol | Unfixed |
| G4 | Header quote 108.09 vs bar close 107.08 | Quote plane and bar plane disagree; unreconciled |

---

## H. Fill-dependent — gated on the 21:30 SGT open

| # | Steps | Expected | Pass |
|---|---|---|---|
| H1 | Market BUY **10** sh, liquid name, via the **strategy** ticket (MANUAL) | Fills. Position `strategy_id=MANUAL-001` — **not** EXTERNAL. Cycle opens **HELD** | ☐ |
| H2 | **Partial** SELL 4 | Qty → 6. Cycle **stays open**. Realized P&L partial, avg cost unchanged, **same `cycle_id`** | ☐ |
| H3 | **Full** SELL 6 | Flat. Cycle **CLOSED**. `cycle_id` retained, not recycled | ☐ |
| H4 | Buy the same name again after H3 | **New** `cycle_id` — a cycle spans flats only within one open→closed run | ☐ |
| H5 | Throughout | `reconcile_drift` stays `[]`; broker net always equals cockpit net | ☐ |

Buy 10 so 4/6 gives a clean partial. Size small for a readable blotter, not for margin.
