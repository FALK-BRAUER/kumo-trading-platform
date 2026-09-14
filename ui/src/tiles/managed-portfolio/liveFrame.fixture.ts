/**
 * THE LIVE FRAME, copied verbatim out of `ui:state:trades` on the paper stack 2026-08-29.
 *
 * Four real trade cycles (one per strategy, all 25 fields intact) and the engine's own
 * `realized_periods` sweep, unmodified. Extracted to its own module because a SECOND hand-copy would
 * be a second derivation of the same fact, free to drift from this one — which is the defect class
 * this directory keeps paying for. Every test that needs a realistic frame imports THIS.
 *
 * Load-bearing properties, asserted by the tests that use it rather than assumed here:
 *   - the live cycles are BCTROT-004, MOMENTUM-002, QC345-003, TECHIVOL-005 — NOT MANUAL-001
 *   - the 1M sweep carries MANUAL-001 +1509.72, second only to MOMENTUM-002 +3085.56
 *   - `all` carries EXTERNAL -1093.30 with unclaimed 0.14 and NO live external position
 */
export const FRAME = {
 "trades": [
  {
   "account_id": "ALPACA-00000000-0000-4000-8000-00000000c0de",
   "client_id": "ALPACA",
   "instrument_id": "AEM.XNYS",
   "strategy_id": "BCTROT-004",
   "cycle_id": "ALPACA-00000000-0000-4000-8000-00000000c0de:ALPACA:AEM.XNYS:BCTROT-004:1787859651075930094",
   "manager_id": null,
   "state": "HELD",
   "side": "LONG",
   "quantity": 9.0,
   "is_capital_deployed": true,
   "is_engaged": true,
   "avg_px_open": 215.09,
   "realized_pnl": "0.00 USD",
   "last_px": 205.65,
   "market_value": 1850.8500000000001,
   "unrealized_pl": -74.705004,
   "unrealized_plpc": -0.038796608689345966,
   "leg_count": 1,
   "opened_ts": 1787859651075930094,
   "closed_ts": null,
   "last_event_ts": 1788000012874020675,
   "working_orders": [
    {
     "client_order_id": "PROT-SELL-AEM-XNYS-3c182de6",
     "side": "SELL",
     "order_type": "TRAILING_STOP_MARKET",
     "quantity": 9.0,
     "leaves_qty": 9.0,
     "price": null,
     "trigger_price": 203.146112,
     "time_in_force": "GTC",
     "status": "ACCEPTED",
     "ts_last": 1787846495391897962
    },
    {
     "client_order_id": "PROT-SELL-AEM-XNYS-8ba2ec77",
     "side": "SELL",
     "order_type": "TRAILING_STOP_MARKET",
     "quantity": 9.0,
     "leaves_qty": 9.0,
     "price": null,
     "trigger_price": 203.102848,
     "time_in_force": "GTC",
     "status": "ACCEPTED",
     "ts_last": 1787859691380320210
    }
   ],
   "broker_protected": true,
   "venue_avg_px": 213.950556,
   "basis_contested": true
  },
  {
   "account_id": "ALPACA-00000000-0000-4000-8000-00000000c0de",
   "client_id": "ALPACA",
   "instrument_id": "AEM.XNYS",
   "strategy_id": "MOMENTUM-002",
   "cycle_id": "ALPACA-00000000-0000-4000-8000-00000000c0de:ALPACA:AEM.XNYS:MOMENTUM-002:1787232907814216924",
   "manager_id": null,
   "state": "HELD",
   "side": "LONG",
   "quantity": 9.0,
   "is_capital_deployed": true,
   "is_engaged": true,
   "avg_px_open": 212.72159003831422,
   "realized_pnl": "-8.29 USD",
   "last_px": 205.65,
   "market_value": 1850.8500000000001,
   "unrealized_pl": -74.705004,
   "unrealized_plpc": -0.038796608689345966,
   "leg_count": 1,
   "opened_ts": 1787232907814216924,
   "closed_ts": null,
   "last_event_ts": 1788000012874020675,
   "working_orders": [
    {
     "client_order_id": "PROT-SELL-AEM-XNYS-3c182de6",
     "side": "SELL",
     "order_type": "TRAILING_STOP_MARKET",
     "quantity": 9.0,
     "leaves_qty": 9.0,
     "price": null,
     "trigger_price": 203.146112,
     "time_in_force": "GTC",
     "status": "ACCEPTED",
     "ts_last": 1787846495391897962
    },
    {
     "client_order_id": "PROT-SELL-AEM-XNYS-8ba2ec77",
     "side": "SELL",
     "order_type": "TRAILING_STOP_MARKET",
     "quantity": 9.0,
     "leaves_qty": 9.0,
     "price": null,
     "trigger_price": 203.102848,
     "time_in_force": "GTC",
     "status": "ACCEPTED",
     "ts_last": 1787859691380320210
    }
   ],
   "broker_protected": true,
   "venue_avg_px": 213.950556,
   "basis_contested": true
  },
  {
   "account_id": "ALPACA-00000000-0000-4000-8000-00000000c0de",
   "client_id": "ALPACA",
   "instrument_id": "AMAT.XNAS",
   "strategy_id": "QC345-003",
   "cycle_id": "ALPACA-00000000-0000-4000-8000-00000000c0de:ALPACA:AMAT.XNAS:QC345-003:1787584200920587011",
   "manager_id": null,
   "state": "HELD",
   "side": "LONG",
   "quantity": 6.0,
   "is_capital_deployed": true,
   "is_engaged": true,
   "avg_px_open": 475.73,
   "realized_pnl": "0.00 USD",
   "last_px": 462.43,
   "market_value": 2774.58,
   "unrealized_pl": -79.80000000000007,
   "unrealized_plpc": -0.027957034452315413,
   "leg_count": 1,
   "opened_ts": 1787584200920587011,
   "closed_ts": null,
   "last_event_ts": 1788000012874020675,
   "working_orders": [
    {
     "client_order_id": "PROT-SELL-AMAT-XNAS-6094b55a",
     "side": "SELL",
     "order_type": "TRAILING_STOP_MARKET",
     "quantity": 6.0,
     "leaves_qty": 6.0,
     "price": null,
     "trigger_price": 450.15912,
     "time_in_force": "GTC",
     "status": "ACCEPTED",
     "ts_last": 1787584253156758841
    }
   ],
   "broker_protected": true,
   "venue_avg_px": 475.73,
   "basis_contested": false
  },
  {
   "account_id": "ALPACA-00000000-0000-4000-8000-00000000c0de",
   "client_id": "ALPACA",
   "instrument_id": "CRM.XNYS",
   "strategy_id": "TECHIVOL-005",
   "cycle_id": "ALPACA-00000000-0000-4000-8000-00000000c0de:ALPACA:CRM.XNYS:TECHIVOL-005:1787932848429946086",
   "manager_id": null,
   "state": "HELD",
   "side": "LONG",
   "quantity": 6.0,
   "is_capital_deployed": true,
   "is_engaged": true,
   "avg_px_open": 261.18,
   "realized_pnl": "0.00 USD",
   "last_px": 256.48,
   "market_value": 1538.88,
   "unrealized_pl": -28.199999999999932,
   "unrealized_plpc": -0.017995252316410096,
   "leg_count": 1,
   "opened_ts": 1787932848429946086,
   "closed_ts": null,
   "last_event_ts": 1788000012874020675,
   "working_orders": [
    {
     "client_order_id": "PROT-SELL-CRM-XNYS-0edf2cf0",
     "side": "SELL",
     "order_type": "TRAILING_STOP_MARKET",
     "quantity": 6.0,
     "leaves_qty": 6.0,
     "price": null,
     "trigger_price": 246.642666,
     "time_in_force": "GTC",
     "status": "ACCEPTED",
     "ts_last": 1787932901687201292
    }
   ],
   "broker_protected": true,
   "venue_avg_px": 261.18,
   "basis_contested": false
  }
 ],
 "realized_periods": {
  "1D": {
   "total": 0,
   "closed_count": 0,
   "adjustments": 0,
   "net": 0,
   "by_strategy": {},
   "unclaimed": 0.0,
   "unmatched": 0,
   "is_partial": false
  },
  "1W": {
   "total": -34.26000000000093,
   "closed_count": 78,
   "adjustments": -1.75,
   "net": -36.01000000000093,
   "by_strategy": {
    "BCTROT-004": -76.57000000000012,
    "MOMENTUM-002": 158.04999999999976,
    "TECHIVOL-005": -57.4700000000002,
    "QC345-003": -45.9200000000001,
    "MANUAL-001": -12.350000000000136
   },
   "unclaimed": 0.0,
   "unmatched": 0,
   "is_partial": false
  },
  "1M": {
   "total": 3959.78,
   "closed_count": 273,
   "adjustments": -1002.57,
   "net": 2957.21,
   "by_strategy": {
    "MANUAL-001": 1509.7199999999996,
    "EXTERNAL": -412.2300000000004,
    "MOMENTUM-002": 3085.5600000000004,
    "BCTROT-004": -120.01999999999998,
    "TECHIVOL-005": -57.4700000000002,
    "QC345-003": -45.9200000000001
   },
   "unclaimed": 0.14000000000000057,
   "unmatched": 0,
   "is_partial": false
  },
  "3M": {
   "total": 3278.71,
   "closed_count": 275,
   "adjustments": -1002.63,
   "net": 2276.08,
   "by_strategy": {
    "EXTERNAL": -1093.3000000000004,
    "MANUAL-001": 1509.7199999999996,
    "MOMENTUM-002": 3085.5600000000004,
    "BCTROT-004": -120.01999999999998,
    "TECHIVOL-005": -57.4700000000002,
    "QC345-003": -45.9200000000001
   },
   "unclaimed": 0.14000000000000057,
   "unmatched": 0,
   "is_partial": false
  },
  "all": {
   "total": 3278.71,
   "closed_count": 275,
   "adjustments": -1002.63,
   "net": 2276.08,
   "by_strategy": {
    "EXTERNAL": -1093.3000000000004,
    "MANUAL-001": 1509.7199999999996,
    "MOMENTUM-002": 3085.5600000000004,
    "BCTROT-004": -120.01999999999998,
    "TECHIVOL-005": -57.4700000000002,
    "QC345-003": -45.9200000000001
   },
   "unclaimed": 0.14000000000000057,
   "unmatched": 0,
   "is_partial": false
  },
  "reconciliation": {
   "implied": 2276.079961999997,
   "reported": 3278.71,
   "adjustments": -1002.6299999999994,
   "residual": 3.8000003769411705e-05,
   "reconciled": true,
   "cash_residual": 1.0913936421275139e-10,
   "verdict": "OK",
   "by_type": {
    "FEE": -12.169999999999995,
    "WH": -990.46
   },
   "phantom_lots": {}
  }
 },
 "realized_session": {
  "by_strategy": {},
  "closed_count": {},
  "total": 0,
  "partial_open": 3,
  "is_partial": true
 },
 "ts": 1788000012895205967,
 "status": "ok",
 "error": null
} as unknown as Record<string, unknown>;
