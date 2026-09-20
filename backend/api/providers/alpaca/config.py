"""Config for the Alpaca Nautilus data client (issue: Alpaca migration).

Paper and live share the same API at different base URLs; the key pair (`APCA_API_KEY_ID` /
`APCA_API_SECRET_KEY`) selects the account. Secrets are injected from the keychain by the launch script —
never committed. `feed` picks the market-data feed: "sip" (paid, full real-time consolidated tape —
active since 2026-08-01) or "iex" (free, thin, single-venue).
"""

from __future__ import annotations

from nautilus_trader.config import LiveDataClientConfig, LiveExecClientConfig


class AlpacaDataClientConfig(LiveDataClientConfig, frozen=True):
    """
    Configuration for ``AlpacaDataClient``.

    Parameters
    ----------
    api_key : str, optional
        Alpaca API key id (``PK…`` for paper). Sourced from ``APCA_API_KEY_ID`` if ``None``.
    api_secret : str, optional
        Alpaca API secret. Sourced from ``APCA_API_SECRET_KEY`` if ``None``.
    trading_base_url : str
        REST base for account/assets. Paper: ``https://paper-api.alpaca.markets``.
    data_base_url : str
        REST base for historical market data.
    ws_base_url : str
        WebSocket base for live streaming (feed appended, e.g. ``…/v2/sip``).
    feed : str, default "sip"
        Market-data feed: ``"sip"`` (paid/full real-time, unlimited WS symbols) or ``"iex"``
        (free/thin, 30 channel-slots, single-venue volume).
    """

    api_key: str | None = None
    api_secret: str | None = None
    trading_base_url: str = "https://paper-api.alpaca.markets"
    data_base_url: str = "https://data.alpaca.markets"
    ws_base_url: str = "wss://stream.data.alpaca.markets/v2"
    feed: str = "sip"
    #: The deployment's symbols the data client SEEDS the cache with at connect (#1057): the declared
    #: universe ∪ the pool ∪ every enabled lane's universe ∪ the compass references, minus exclusions
    #: — IBKR's `load_contracts` set. COMPUTED AT BUILD TIME by `build_data`, outside any event loop,
    #: because `_pool_symbols` refuses from inside one (measured 2026-09-13 08:19Z: the first version
    #: read it from `_connect`, the pool was refused, and the 11 pool names stayed unseeded while the
    #: log said "270 seeded"). Empty means the build could not read anything — say so, never treat
    #: it as "nothing to seed".
    seed_symbols: tuple[str, ...] = ()


class AlpacaExecClientConfig(LiveExecClientConfig, frozen=True):
    """
    Configuration for ``AlpacaExecutionClient``.

    Parameters
    ----------
    api_key : str, optional
        Alpaca API key id. Sourced from ``APCA_API_KEY_ID`` if ``None``.
    api_secret : str, optional
        Alpaca API secret. Sourced from ``APCA_API_SECRET_KEY`` if ``None``.
    trading_base_url : str
        REST base for orders/positions/account. Paper: ``https://paper-api.alpaca.markets``.
    data_base_url : str
        REST base (shared http client also serves data; unused by exec but kept for one client).
    """

    # No `ws_base_url` here (#652 item 8b): the exec path has no WebSocket — fills arrive via
    # reconciliation. The knob was accepted, documented and forwarded while NOTHING read it; a dead
    # knob reads as configuration while being none. Reintroduce it WITH the trade-updates stream,
    # not before. (The data config's `ws_base_url` above is live and stays.)
    api_key: str | None = None
    api_secret: str | None = None
    trading_base_url: str = "https://paper-api.alpaca.markets"
    data_base_url: str = "https://data.alpaca.markets"
