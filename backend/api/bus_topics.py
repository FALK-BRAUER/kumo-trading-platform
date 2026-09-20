"""Broker bus topics — the NEUTRAL contract between exec clients and the engine (#608).

A topic name is a contract, not a vendor. These lived in `api.providers.alpaca.exec_client`, which
made the ENGINE import the Alpaca connector just to learn what to subscribe to (#608 instance 2) —
and would make a second exec client either import Alpaca too or re-spell the strings and drift.
One definition, below both, importing nothing.

Any exec client publishing account/reconcile/equity state uses these names; the engine subscribes
to them without knowing who publishes. The cadences and payload documentation stay with the
publisher (`api/providers/alpaca/exec_client.py`) — this module owns only the names.
"""

from __future__ import annotations

#: Broker account snapshot (equity, buying power, currency).
ACCOUNT_TOPIC = "broker.account"
#: Broker-vs-cache position drift (#26 reconciliation banner) — the exec client is the only
#: component that sees BOTH the broker and the Nautilus cache.
RECONCILE_TOPIC = "broker.reconcile"
#: Orders reconciliation had to SKIP because no OrderStatusReport can represent them (#643).
#: Published every batch, empty included: [] states known-clean; never seeing the topic is "never asked".
UNRECONCILED_TOPIC = "broker.unreconciled_orders"
#: Equity curve (#243) — its own topic, its own much slower cadence.
EQUITY_TOPIC = "broker.equity_curve"
