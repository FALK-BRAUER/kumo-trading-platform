"""Venue-neutral asset metadata off `Instrument.info` (#624/#647).

A strategy reading `info["stockType"]` would be learning IBKR trivia instead of Alpaca trivia — the
same #608 violation with a different vendor's name on it. This is the ONE place that knows both
`info` shapes; everything above it asks in neutral vocabulary and never learns which venue answered.

The shapes, verified against the INSTALLED packages (2026-08-29), not docs or memory:

  IBKR    `contract_details_to_dict` (nautilus_trader/adapters/interactive_brokers/parsing/
          instruments.py) flattens `ContractDetails`: `longName`, `stockType`, `fundName`,
          `fundType`, `fundAssetType` at the top level; the `Contract` nested under `"contract"`
          with `primaryExchange`. `stockType` is the venue's EXPLICIT classification ("COMMON",
          "ETF", "ETN", "REIT", "ADR", "CLOSED-END FUND", ...) — better than any name heuristic.
  Alpaca  api/providers/alpaca/providers.py:parse_equity sets `info={"name": ...}` — the name only.
          Alpaca's /v2/assets carries no classification, so `asset_type` is honestly None there.

THREE STATES, NEVER TWO. `None` in any slot means "this venue never told us", which is not a
variant of any real answer. A caller must report an all-None instrument as unclassifiable, never
default it into a ranking — absence must not be readable as permission (2026-08-29).
"""

from __future__ import annotations

#: MIC (ISO 10383, what cache instrument ids carry) -> the operating name venues and reference data
#: use. Needed because `is_fundamental_like_asset` (kumo-strategies) short-circuits on
#: exchange == "ARCA" — the MIC "ARCX" would silently never fire that rule.
_MIC_TO_EXCHANGE: dict[str, str] = {
    "XNAS": "NASDAQ",
    "XNYS": "NYSE",
    "ARCX": "ARCA",
    "XASE": "AMEX",
    "BATS": "BATS",
    "OTCM": "OTC",
}

#: IBKR `stockType` markers. ETNs are exchange-traded PRODUCTS, not companies — same bucket as ETF
#: for a stock-selection universe. Anything carrying "FUND" (CLOSED-END FUND, OPEN-END FUND, ...)
#: is a fund.
_ETF_TYPES = ("ETF", "ETN")


def _text(value) -> str | None:
    """A non-empty stripped string, or None — '' is absence, not an answer."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def asset_meta(instrument) -> dict:
    """`{"name", "asset_type", "exchange"}` for one cached Nautilus instrument, venue-neutrally.

    `asset_type` is "ETF" | "FUND" | "EQUITY" | None — None meaning "this venue cannot say"
    (Alpaca), never a guess. `name` is the venue's display name (IBKR `fundName`/`longName`,
    Alpaca `name`), or None. `exchange` is the venue's own exchange code when it names one (IBKR
    `contract.primaryExchange`), else the instrument id's MIC mapped to its operating name.
    """
    if instrument is None:
        return {"name": None, "asset_type": None, "exchange": None}

    info = getattr(instrument, "info", None) or {}
    name: str | None = None
    asset_type: str | None = None
    exchange: str | None = None

    if "stockType" in info or "longName" in info or "contract" in info:
        # IBKR shape (contract_details_to_dict). fundName first: for funds it is the honest display
        # name and its presence is itself a classification.
        name = _text(info.get("fundName")) or _text(info.get("longName"))
        stock_type = _text(info.get("stockType"))
        if stock_type is not None:
            upper = stock_type.upper()
            if upper in _ETF_TYPES:
                asset_type = "ETF"
            elif "FUND" in upper:
                asset_type = "FUND"
            else:
                asset_type = "EQUITY"
        elif _text(info.get("fundName")) or _text(info.get("fundType")):
            asset_type = "FUND"
        exchange = _text((info.get("contract") or {}).get("primaryExchange"))
    elif "name" in info:
        # Alpaca shape: a name and nothing else. No classification exists, so none is invented.
        name = _text(info.get("name"))

    if exchange is None:
        mic = getattr(getattr(getattr(instrument, "id", None), "venue", None), "value", None)
        if mic:
            exchange = _MIC_TO_EXCHANGE.get(mic, mic)

    return {"name": name, "asset_type": asset_type, "exchange": exchange}
