"""Custom Nautilus `Data` types (#182 follow-up) — the proper Nautilus-native extension point for
enrichment data an algorithm should be able to subscribe to, as distinct from the engine→Redis→WS path
that only serves the UI. Uses `nautilus_trader.model.custom.customdataclass`, the documented mechanism for
generic data: gives `to_dict`/`from_dict`/`to_bytes`/`from_bytes`/`to_arrow`/`from_arrow` for free, and
registers with Nautilus's own serialization + Arrow systems automatically (msgpack + `pa.RecordBatch`).

NOT catalog-write-capable as-is (codex review: this claim was overstated in an earlier draft) — writing to
a `ParquetDataCatalog` needs `customdataclass_pyo3` instead (`nautilus_trader/model/custom.py`), a
different decorator this file doesn't use. Switch to it, with a real catalog smoke test, only if/when
historical fundamentals replay for a backtest becomes an actual requirement — not before.

Fields are plain `float`, NOT `float | None` — `@customdataclass`'s Arrow-schema generator only supports
a fixed set of concrete types (confirmed empirically: `float | None` raises `TypeError: Unsupported custom
data field type`). Missing values use `float("nan")` instead, the standard float-missing-value convention
(same one pandas/numpy use) — `math.isnan(x)` at every read site, never `is None`.
"""

import math

from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass
from nautilus_trader.model.data import DataType
from nautilus_trader.model.identifiers import InstrumentId


@customdataclass
class FundamentalsData(Data):
    """Company fundamentals for one instrument (#182 follow-up, watchlist KPI Phase 2) — Market Cap, Beta,
    EPS (trailing annual), trailing P/E, Dividend Amount (last paid). Same values `UiFeedStrategy`
    publishes to Redis for the UI, ALSO published here via `Actor.publish_data` so any Nautilus `Strategy`
    registered on the same node can `subscribe_data(fundamentals_data_type(iid), instrument_id=iid)` and
    receive live updates through `on_data` — a live algorithm reading these KPIs, not just the UI.

    NOT written to a `ParquetDataCatalog` (no backtest/historical replay) — that's a separate, larger step
    (a strategy would need to actually consume this as a backtest input first; see engine_node.py's
    `_refresh_fundamentals` docstring and the codex consult that scoped this decision)."""

    instrument_id: InstrumentId
    market_cap: float
    beta: float
    eps: float
    pe: float
    dividend_amount: float
    as_of: str


def fundamentals_data_type(instrument_id: str) -> DataType:
    """The per-instrument `DataType` for `FundamentalsData` — same shape used by both the publisher
    (`UiFeedStrategy._refresh_fundamentals`) and any subscriber, so publish/subscribe topics always match
    (Nautilus derives the msgbus topic from `DataType.metadata` when present — confirmed empirically)."""
    return DataType(FundamentalsData, metadata={"instrument_id": instrument_id})


def nan_if_none(value: float | None) -> float:
    """`None` → `float("nan")` for a `FundamentalsData` field — the Arrow-schema-safe missing-value
    convention `@customdataclass` requires (see module docstring)."""
    return math.nan if value is None else value
