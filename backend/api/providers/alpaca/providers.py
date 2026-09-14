"""Alpaca instrument provider — builds Nautilus `Equity` instruments from Alpaca's `/v2/assets`.

Instrument ids are canonical `TICKER.MIC` (matching the rest of the cockpit): Alpaca's `exchange`
field is mapped to its ISO-10383 MIC so streamed bars carry the same ids as positions/watchlist.
"""

from __future__ import annotations

from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Price, Quantity

from api.providers.alpaca.http import AlpacaHttpClient

# Alpaca `exchange` → MIC (ISO 10383). Covers the US-equity venues the cockpit trades.
EXCHANGE_TO_MIC: dict[str, str] = {
    "NASDAQ": "XNAS",
    "NYSE": "XNYS",
    "ARCA": "ARCX",
    "AMEX": "XASE",
    "BATS": "BATS",
    "OTC": "OTCM",
}

_PRICE_INCREMENT = Price.from_str("0.01")  # standard US-equity tick (≥ $1)


def parse_equity(asset: dict, ts: int = 0) -> Equity | None:
    """One Alpaca asset dict → a Nautilus `Equity`, or None if unmappable (venue/class not supported)."""
    if asset.get("class") != "us_equity":
        return None
    mic = EXCHANGE_TO_MIC.get(asset.get("exchange", ""))
    if mic is None:
        return None
    symbol = asset["symbol"]
    # Company name rides in `info` (Nautilus `Equity` has no name field). `info` is the documented slot for
    # adapter/venue metadata — JSON-serializable, ignored by id/precision consumers. Feeds symbol-search (#25)
    # and any future name-aware UI. Blank/missing name → fall back to the symbol so it's never null.
    name = (asset.get("name") or "").strip() or symbol
    return Equity(
        instrument_id=InstrumentId(Symbol(symbol), Venue(mic)),
        raw_symbol=Symbol(symbol),
        currency=USD,
        price_precision=2,
        price_increment=_PRICE_INCREMENT,
        lot_size=Quantity.from_int(1),  # Alpaca supports fractional/1-share lots
        ts_event=ts,
        ts_init=ts,
        info={"name": name},
    )


class AlpacaInstrumentProvider(InstrumentProvider):
    """Loads the tradable US-equity universe from Alpaca into the cache."""

    def __init__(self, client: AlpacaHttpClient, config=None) -> None:
        super().__init__(config=config)
        self._client = client

    async def load_all_async(self, filters: dict | None = None) -> None:
        assets = await self._client.list_assets()
        for asset in assets:
            if not asset.get("tradable"):
                continue
            instrument = parse_equity(asset)
            if instrument is not None:
                self.add(instrument)
