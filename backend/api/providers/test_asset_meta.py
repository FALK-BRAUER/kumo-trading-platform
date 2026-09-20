"""The venue-neutral asset-metadata accessor (#624/#647).

A strategy reading `Instrument.info["stockType"]` would be learning IBKR trivia instead of Alpaca
trivia — the same #608 violation with a different vendor's name on it. Normalization belongs BELOW
the strategy: ONE accessor in the provider layer that knows both `info` shapes and answers in
neutral vocabulary, three states not two — "this is an ETF", "this is not", and "this venue cannot
tell me" (None), which the caller must report rather than silently rank.

The shapes are pinned from the INSTALLED packages, not from memory (verified 2026-08-29):

  IBKR    `contract_details_to_dict` (nautilus_trader/adapters/interactive_brokers/parsing/
          instruments.py) flattens `ContractDetails` — `longName`, `stockType`, `fundName`,
          `fundType` at the TOP level, the `Contract` nested under `"contract"` with
          `primaryExchange`. `stockType` is an EXPLICIT classification ("COMMON", "ETF", "ETN", ...).
  Alpaca  api/providers/alpaca/providers.py:parse_equity builds `info={"name": ...}` — the name
          only, no classification.
"""

from __future__ import annotations

from nautilus_trader.model.currencies import USD
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Price, Quantity

from api.providers.asset_meta import asset_meta


def _equity(symbol: str, mic: str, info: dict | None) -> Equity:
    return Equity(
        instrument_id=InstrumentId(Symbol(symbol), Venue(mic)),
        raw_symbol=Symbol(symbol),
        currency=USD,
        price_precision=2,
        price_increment=Price.from_str("0.01"),
        lot_size=Quantity.from_int(1),
        ts_event=0,
        ts_init=0,
        info=info,
    )


def test_the_fixture_shapes_are_the_production_shapes():
    """FIXTURE PROPERTY FIRST: the Alpaca shape below must be byte-what parse_equity emits, or every
    assertion against it is about a double production never produces."""
    from api.providers.alpaca.providers import parse_equity

    inst = parse_equity({"class": "us_equity", "symbol": "AAPL", "exchange": "NASDAQ",
                         "name": "Apple Inc. Common Stock"})
    assert inst is not None and inst.info == {"name": "Apple Inc. Common Stock"}


def test_ALPACA_shape_yields_the_name_and_CANNOT_classify():
    meta = asset_meta(_equity("AAPL", "XNAS", {"name": "Apple Inc. Common Stock"}))
    assert meta["name"] == "Apple Inc. Common Stock"
    # Three states: Alpaca's info carries no classification, so the third state — never-told-us —
    # must come back as itself, not as "EQUITY".
    assert meta["asset_type"] is None
    assert meta["exchange"] == "NASDAQ"


def test_IBKR_shape_yields_longName_and_the_EXPLICIT_classification():
    meta = asset_meta(_equity("AAPL", "XNAS", {
        "longName": "APPLE INC", "stockType": "COMMON", "industry": "Technology",
        "contract": {"primaryExchange": "NASDAQ", "symbol": "AAPL"},
    }))
    assert meta == {"name": "APPLE INC", "asset_type": "EQUITY", "exchange": "NASDAQ"}


def test_IBKR_ETF_and_ETN_classify_as_ETF_and_funds_as_FUND():
    etf = asset_meta(_equity("SPY", "ARCX", {
        "longName": "SPDR S&P 500 ETF TRUST", "stockType": "ETF",
        "contract": {"primaryExchange": "ARCA"},
    }))
    assert etf["asset_type"] == "ETF"
    etn = asset_meta(_equity("VXX", "BATS", {"longName": "X", "stockType": "ETN",
                                             "contract": {"primaryExchange": "BATS"}}))
    assert etn["asset_type"] == "ETF"
    fund = asset_meta(_equity("PTY", "XNYS", {
        "longName": "PIMCO CORPORATE & INCOME OPPORTUNITY FUND", "stockType": "CLOSED-END FUND",
        "fundName": "PIMCO Corporate & Income Opportunity Fund",
        "contract": {"primaryExchange": "NYSE"},
    }))
    assert fund["asset_type"] == "FUND"


def test_ABSENT_metadata_is_the_third_state_not_a_quiet_equity():
    """`info=None` and `info={}` are venues that never told us — every field None, so the CALLER
    can see unclassifiable and report it, instead of ranking a nameless instrument."""
    for info in (None, {}):
        meta = asset_meta(_equity("ZZZ", "XNAS", info))
        assert meta["name"] is None and meta["asset_type"] is None, (
            f"info={info!r} produced {meta} — absence was rendered as an answer"
        )
    none = asset_meta(None)
    assert none == {"name": None, "asset_type": None, "exchange": None}


def test_the_exchange_falls_back_to_the_MIC_mapping_when_the_venue_does_not_name_one():
    """`is_fundamental_like_asset` (kumo-trading-strategies) short-circuits on exchange == "ARCA". Cache
    instrument ids carry the MIC ("ARCX"), so without this mapping the venue rule would silently
    never fire on Alpaca-shaped instruments — agreement-is-not-connection, exchange edition."""
    meta = asset_meta(_equity("SPY", "ARCX", {"name": "SPDR S&P 500 ETF Trust"}))
    assert meta["exchange"] == "ARCA"
