"""Provider-agnostic contracts for registering Nautilus data + execution clients.

Mapping principle (vendor-agnostic): the canonical instrument id is `TICKER.MIC` (ISO 10383). Each
vendor maps its native symbology onto that id with its OWN built-in method (IBKR
`convert_exchange_to_mic_venue`, Databento's native MIC venues) — no hand-coded tables. The DATA
provider owns the instrument definition; the EXEC provider references positions by the same id.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The two things a venue's 1-DAY aggregation can cover (#616). Closed set on purpose: a typo
#: recorded as data ("RTH", "regular") would read as the OTHER value at every `== "rth"` site.
DAILY_BARS_COVER = ("rth", "extended")
#: The price series a venue's historical bars can be served in (#1124). `raw` = executable prices,
#: what every backtest ran on; `split` = split-adjusted, what CRSISHORT was measured on. Never
#: `all` (dividends folded in — a third series nobody measured). A venue declares the SET it can
#: serve: Alpaca both, per request; IB `split` only (its TRADES bars are adjusted, the parameter
#: is ignored). A lane that needs one reads the declaration; it does not read the provider's name.
PRICE_ADJUSTMENTS = ("raw", "split")


@dataclass(frozen=True)
class DataClientSpec:
    """What the node needs to wire one provider's live data client (the instrument authority)."""

    client_id: str  # Nautilus ClientId, e.g. "DATABENTO" — requests are pinned here so its definitions win
    config: object  # the provider's Nautilus *DataClientConfig
    factory: type  # the provider's *LiveDataClientFactory
    #: How many symbols may hold LIVE trade/quote/native-bar subscriptions on this provider.
    #:
    #: THE PROVIDER DECLARES ITS OWN LIMITS, and a provider that declares none is not limited (#619).
    #: This was an Alpaca free-plan constant read from the strategy, so `realtime_symbol_budget("iex")`
    #: rationed EVERY provider: ibkr-paper-retired denied 87 symbols live data against a competitor's pricing
    #: tier, logging `feed=iex` on a node holding no Alpaca credential.
    #:
    #: `inf` is the safe default in the sense that matters here — a foreign cap silently starving a feed
    #: is far worse than none, and a venue's real limits belong to its adapter (IBKR: tick-by-tick
    #: entitlement/concurrency #578 and historical pacing #617, both enforced below the connector).
    realtime_symbol_budget: float = float("inf")
    #: How many HISTORICAL bar requests per minute this provider will actually serve.
    #:
    #: IBKR documents ~60 per 10 minutes and enforces it by answering with EMPTY ARRAYS rather than
    #: erroring — 744 requests in one 45s window produced 2,232 empty responses, 0 bars and 0 errors
    #: on staging (#617). A rate limiter that refuses by returning success is indistinguishable from
    #: a venue with no data, which is why nothing detected it for a whole session.
    #:
    #: `inf` for a provider that declares none, same direction as `realtime_symbol_budget` and for
    #: the same reason: a foreign limit silently throttling a feed is worse than no limit.
    historical_requests_per_minute: float = float("inf")
    #: Whether this provider's instruments carry the venue's own SESSION SCHEDULE (#628).
    #:
    #: IBKR's `Instrument.info` holds `liquidHours` + `timeZoneId` — the exchange's real sessions with
    #: CLOSED days marked, so weekends and holidays read identically and it is holiday-aware for
    #: free. Alpaca's `info` carries only `{"name": ...}` and cannot supply one.
    #:
    #: DECLARED, NOT INFERRED FROM THE PROVIDER NAME. `if data_provider == "ibkr"` would work today
    #: and silently skip the next adapter — the #619 defect, and what #608 exists for.
    supplies_trading_calendar: bool = False
    #: What SESSION SPAN this provider's 1-DAY bars aggregate over (#616): "rth" (the regular
    #: 09:30–16:00 session) or "extended" (pre/post-market folded into the daily OHLC).
    #:
    #: The same `1d` bar type is defined OPPOSITELY by our two production providers — verified from
    #: the sources, not the docs of record in anyone's head:
    #:
    #:   IBKR    "extended": `providers/ibkr.py` sets `use_regular_trading_hours=False`, and the
    #:           installed adapter forwards it into every historical bar request, daily included
    #:           (nautilus_trader/adapters/interactive_brokers/data.py:625 `use_rth=...`; config.py:
    #:           "If True, will request data for Regular Trading Hours only. Only applies to bar data").
    #:   Alpaca  "rth": `/v2/stocks/{sym}/bars` has no session parameter, and Alpaca's aggregation
    #:           table (Market Data FAQ, "How are bars aggregated?") marks conditions `T`/`U`
    #:           (extended-hours trades) as updating a DAILY bar's volume but never its OHLC.
    #:
    #: So a premarket gap-reversal lands inside the IBKR daily bar and not the Alpaca one; two
    #: tenants charting one symbol legitimately disagree and neither looks wrong. Anything grading
    #: indicators over daily bars (the compass' ADX/Ichimoku) inherits the difference.
    #:
    #: REQUIRED, NO DEFAULT, keyword-only — deliberately, the `reserves_shares_against_resting_stop`
    #: shape: a default is what made #574 invisible, and either wrong value here is plausible-looking
    #: (the numbers differ by a premarket move, not by an obvious break). A new provider must read
    #: its venue's aggregation semantics and declare, not inherit. Keyed identically in the measured
    #: venue catalogue (api/venues/facts.py: daily_bars_cover); a test pins the two derivations equal.
    daily_bars_cover: str = field(kw_only=True)
    #: Whether this venue STREAMS TRADE TICKS to us (#612).
    #:
    #: Not a capability of the venue in the abstract — a property of what THIS account actually
    #: receives. IB serves market data to one session per user; the live session holds it, so the
    #: paper gateway gets `10197: No market data during competing live session` and no tape ever
    #: arrives, while exec works perfectly.
    #:
    #: It matters because an INTERNAL bar type is aggregated FROM trade ticks: Nautilus routes it to
    #: a `TimeBarAggregator` that subscribes to them, so a venue with no tape yields an empty series
    #: with no error. Measured 2026-08-31: staging's h1/w1 were never subscribed, four sparklines
    #: were blank, and 11 of 22 held positions had no price because the bar fallback had nothing.
    #:
    #: REQUIRED, NO DEFAULT, keyword-only — the `daily_bars_cover` shape, for the same reason. A
    #: default is what let this be assumed: the split was written for a venue that had ticks and
    #: nothing recorded that the fix depended on it.
    streams_trade_ticks: bool = field(kw_only=True)

    #: Does this venue stream an NBBO QUOTE tape (#812)?
    #:
    #: A SEPARATE FACT FROM `streams_trade_ticks`, and it has to be. Alpaca serves both; this IBKR
    #: entitlement serves neither; a venue could serve one and not the other. One flag standing for
    #: two planes is a derivation that cannot be right about both.
    #:
    #: MEASURED, ibkr-paper boot 2026-09-09 05:36 UTC: `_after_definition` subscribed BOTH planes for
    #: every instrument — 112 x 2 = 224 tick-by-tick requests — and IB answered with 135 x 10189 (no
    #: market data permissions) and 199 x 10190 (max tick-by-tick requests reached), delivering zero
    #: ticks. IB's tick-by-tick concurrency limit is small and shared, so the unservable requests
    #: consume it: a symbol that COULD have been served was denied a slot.
    #:
    #: REQUIRED, NO DEFAULT, keyword-only — the `streams_trade_ticks` shape, for its stated reason. A
    #: default is what lets a provider added later be ASSUMED to serve quotes, which is how the
    #: subscription storm above got written in the first place.
    streams_quote_ticks: bool = field(kw_only=True)
    #: Which PRICE SERIES this client's historical bars can be served in (#1124): a non-empty subset
    #: of `PRICE_ADJUSTMENTS`. Measured per venue (Alpaca 2026-09-18: NVDA 2024-06-07 reads 1208.88
    #: raw / 120.89 split / 120.54 all — `split` equals IB's TRADES bar). A lane that was measured on
    #: split-adjusted bars refuses a client whose set lacks `split` — BY THE DECLARATION, never by
    #: the provider's name, which is what `crsi_short.py` used to check and what made the lane
    #: unbuildable on Alpaca for a fact Alpaca can serve.
    #:
    #: REQUIRED, NO DEFAULT, keyword-only — the `daily_bars_cover` shape, for the same reason: a
    #: default here would be assumed by the next provider, and both wrong values look plausible.
    #: Keyed identically in the venue catalogue (api/venues/facts.py); a test pins the two equal.
    price_adjustments: frozenset[str] = field(kw_only=True)
    #: SHORTABILITY, where the venue supplies it (#857). A factory `(publish, now_ns) -> plane | None`
    #: declared by the connector; the plane answers `health(now_ns)`, `borrow_rates(now_ns)` and
    #: `subscribe(instrument)` (a coroutine). None on every venue that has no such plane — and that
    #: None is what the engine reads, never an IB import above the connector (test_import_boundary).
    shortable_plane: object | None = None


    def __post_init__(self) -> None:
        # Refuse, never record: a garbage declaration ("RTH", "regular") stored as data would read
        # as the OTHER value at every comparison site — a silent wrong answer, the #574 family.
        if self.daily_bars_cover not in DAILY_BARS_COVER:
            raise ValueError(
                f"daily_bars_cover must be one of {DAILY_BARS_COVER}, got "
                f"{self.daily_bars_cover!r} — declare what the venue's 1-day aggregation actually "
                f"covers (#616), do not guess"
            )
        adj = self.price_adjustments
        if not isinstance(adj, frozenset) or not adj or not adj <= set(PRICE_ADJUSTMENTS):
            raise ValueError(
                f"price_adjustments must be a non-empty frozenset within {PRICE_ADJUSTMENTS}, got "
                f"{adj!r} — declare which series the venue's historical bars can be served in "
                f"(#1124); `all`/`dividend` are not series this platform serves"
            )


@dataclass(frozen=True)
class ExecClientSpec:
    """What the node needs to wire one provider's live execution client."""

    client_id: str  # Nautilus ClientId, e.g. "INTERACTIVE_BROKERS"
    config: object  # the provider's Nautilus *ExecClientConfig
    factory: type  # the provider's *LiveExecClientFactory
    #: Whether this venue REFUSES a reducing order while another resting order already reserves the
    #: shares. Alpaca does — `insufficient qty available (available: 0)`, #358 — so the exit path must
    #: cancel-and-await before submitting; IB has no such reservation concept at all (#430), so the
    #: availability wait must be SKIPPED there rather than timing out and refusing every exit (#641).
    #:
    #: REQUIRED, NO DEFAULT, deliberately: an absent fact and a false one read the same to a human and
    #: opposite at the call site (api/venues/facts.py records the IB entry as False for exactly this
    #: reason). A new provider must MEASURE and declare, not inherit a guess — either wrong value has a
    #: concrete cost (True on a non-reserving venue: dead exits behind a pointless wait; False on a
    #: reserving one: exits rejected into live reservations).
    #:
    #: DECLARED, NOT INFERRED FROM THE PROVIDER NAME (#619/#608), and keyed identically to the measured
    #: venue catalogue (`api/venues/facts.py: reserves_shares_against_resting_stop`) — a test pins the
    #: two derivations equal.
    reserves_shares_against_resting_stop: bool
    #: The venue's REFERENCE-ASSET LEDGER (#647), or None when this venue has no such thing.
    #:
    #: Nautilus does not model delisting: no `Instrument.info` shape says "this name stopped
    #: existing", so a strategy that must notice a HELD name delisting (qc345's `terminal_buckets`
    #: reads a `status` column) genuinely needs a reference source. That source is a PROVIDER
    #: capability: a zero-arg callable returning the venue's asset rows (dicts carrying at least
    #: `symbol` and `status`, unfiltered — a delisted name is exactly the row that matters), built
    #: from the provider's OWN configured endpoint. Alpaca serves it from `trading_base_url`; the
    #: previous shape was a URL hardcoded in the lanes layer, which sent every account's keys to
    #: `paper-api` and raised `assets are required` on venues holding no Alpaca credential.
    #:
    #: THREE STATES at the consumer, never two: rows (detection armed), a callable that RAISES
    #: (ledger unreadable — reported as its own loud condition), and None (no ledger on this venue —
    #: "delisting detection OFF", said at ERROR, not inferred from silence). IBKR declares None.
    reference_assets: object | None = None
