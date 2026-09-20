"""IBKR provider — registers Nautilus's native Interactive Brokers execution client.

Execution + account/positions only (market data is Databento). Connects to an IB Gateway running in
Docker (brought up separately, headless, paper) — see the ibkr-docker-gateway memory. On connect the
client reconciles the account's positions into the Nautilus cache, which the portfolio tile reads.

NB: the gateway must NOT be in read-only API mode — Nautilus's exec client handshake needs write access
even just to reconcile (IB error 321 otherwise). The cockpit submits no orders, so on paper this is safe.
"""

from __future__ import annotations

import contextlib
import logging
import os

from nautilus_trader.adapters.interactive_brokers.common import IB, IBContract
from nautilus_trader.adapters.interactive_brokers.config import (
    IBMarketDataTypeEnum,
    InteractiveBrokersDataClientConfig,
    InteractiveBrokersExecClientConfig,
    InteractiveBrokersInstrumentProviderConfig,
)
from nautilus_trader.adapters.interactive_brokers.factories import (
    InteractiveBrokersLiveDataClientFactory,
    InteractiveBrokersLiveExecClientFactory,
)

from api.providers.base import DataClientSpec, ExecClientSpec

_log = logging.getLogger("kumo.providers.ibkr")


#: WHY A SYMBOL-KEYED OVERRIDE AND NOT AN EXCHANGE-KEYED ONE (#622).
#:
#: The obvious fix is a table of the exchanges Nautilus cannot map — `exchange_to_mic_venue` returns
#: None for AMEX (ISO: XASE), ISLAND, NYSENAT and PSE. That was built first and then DELETED, because
#: it can never run: the adapter resolves the venue itself and falls back silently,
#:
#:     providers.py:919-925   if convert_exchange_to_mic_venue:
#:                                venue = exchange_to_mic_venue(exchange)
#:                                if venue: return venue
#:                            return exchange        <-- the raw string, no error
#:
#: so `TRT.AMEX` exists before any cockpit code sees the instrument, and nothing downstream is ever
#: handed an exchange string to re-resolve. `symbol_to_mic_venue` is checked FIRST
#: (providers.py:825-829) and is the only supported way in.
#:
#: staging holds exactly one such instrument — `TRT.AMEX` beside `GORO.XASE`, two identities for one
#: exchange — and paper holds none. An id is what positions, cycles, claims and reconciliation are
#: keyed on, which is why one row is worth this much comment.

#: Per-instance symbol -> MIC, `SYMBOL=MIC,SYMBOL=MIC`.
_SYMBOL_MIC_ENV = "KUMO_IBKR_SYMBOL_MIC"


def _symbol_mic_overrides() -> dict[str, str]:
    """Symbol -> MIC handed to Nautilus's `symbol_to_mic_venue` (#622).

    THIS IS THE ONLY LEVER ON THE ADAPTER'S OUTPUT, and that is why it exists in this shape rather
    than as the exchange-keyed table one would expect. The adapter mints the venue itself:

        providers.py:919-925  _resolve_venue_from_exchange(exchange):
                                  venue = exchange_to_mic_venue(exchange)
                                  if venue: return venue
                                  return exchange          <-- SILENT raw-string fallback

    `TRT.AMEX` is created there, before any cockpit code sees the instrument, so an exchange-keyed
    resolver on our side can never prevent it. `symbol_to_mic_venue` is checked FIRST
    (providers.py:825-829) and is the only supported way in.

    THE KEY IS A PREFIX, NOT A SYMBOL. `providers.py:836` matches
    `contract.symbol.startswith(symbol_prefix)`, so an entry for `TRT` ALSO captures TRTX, TRTN and
    anything else sharing those letters — silently re-pointing the venue of instruments nobody
    intended to touch, which is the same second-identity failure this override exists to fix arriving
    from the other direction. We cannot change Nautilus's matching, so a short key is WARNED about
    here, where the value is read.

    Empty and unset mean the same thing — nothing overridden. Compose interpolates an unset variable
    to the empty string (#581), so the two are indistinguishable downstream and must behave alike.

    No default entries, deliberately. An override is for an instrument an operator has SEEN; shipping
    a guess is what this module refuses to do everywhere else.
    """
    out: dict[str, str] = {}
    raw = (os.environ.get(_SYMBOL_MIC_ENV) or "").strip()
    for entry in (e for e in raw.split(",") if e.strip()):
        symbol, _, mic = entry.partition("=")
        symbol, mic = symbol.strip().upper(), mic.strip().upper()
        if not symbol or not mic:
            _log.warning("IBKR: ignoring malformed %s entry %r (want SYMBOL=MIC)", _SYMBOL_MIC_ENV, entry)
            continue
        if len(symbol) <= 2:
            _log.warning(
                "IBKR: %s key %r is very short and Nautilus matches it as a PREFIX — it will also "
                "capture every other symbol starting with those letters and re-point their venue.",
                _SYMBOL_MIC_ENV, symbol)
        out[symbol] = mic
    return out


#: Per-REQUEST wait for a contract qualification, in seconds (#525). The adapter defaults it to 60
#: and resolution is SERIAL, so one unresolvable symbol costs a full minute of the 60s connect
#: budget — and `_connect()` qualifies every contract BEFORE reporting connected, so busting that
#: budget makes `kernel.py` skip `_trader.start()`: the node comes up with engines running and ZERO
#: strategies.
#:
#: Measured on an IBKR paper instance 2026-08-29: connect used 17.6s of 60s — 41s of headroom — while five bad
#: tickers sat in the live pool (BLLLN, FTNR, IQVIA, JEPO, OVVI; three are transcription errors for
#: BLLN, FTNT and IQV). At the adapter's default, any ONE of them busts the budget alone.
#:
#: 5s rather than 1s: IB answers a healthy qualification in well under a second, but a cold gateway
#: and a busy socket are real, and a bound so tight that a slow answer becomes a missing instrument
#: is the same zero-strategy boot by another route. This bounds the COST of a bad symbol; it does
#: not decide which symbols are bad — that is what a denylist does badly, since it drifts and the
#: source that actually knows resolves only AFTER connect.
#: Per-request wait for the IB client, in seconds (#525). ONE NUMBER, SEVEN PATHS — list them,
#: because the binding constraint is NOT the path this ticket is about:
#:
#:     contract details / qualification   client/contract.py:73,79,116,157   CONNECT PATH
#:     historical BARS, per segment       data.py:626 -> market_data.py:731          <- BINDING
#:     historical quote / trade ticks     data.py:434,473 (no cockpit caller today)
#:     get_open_orders                    client/order.py:154-156            connect + every 5s
#:     get_executions                     client/order.py:217-219
#:     get_positions                      client/account.py:169,171          connect path
#:
#: READ data.py, NOT market_data.py. `market_data.py:591,669` declare `timeout: int = 60` as a
#: PARAMETER DEFAULT, and the live path always overrides it — the adapter's own `data.py:434,473,626` pass
#: `timeout=self._client._request_timeout_secs` into every historical request. A first version of
#: this comment claimed bars were unaffected, with a test that grepped the file where the number is
#: defaulted instead of the file where it is passed: green today, green under exactly the change it
#: claimed to watch, and asserting nothing. Bars ARE bounded by this value.
#:
#: MEASURED on the 2026-08-29 staging boot rather than reasoned about — 236 paired historical bar
#: request/response cycles: median 0.94s, p99 1.70s, MAX 1.97s, none above 3s. Qualification took
#: 17.2s for ~126 contracts (~137ms each) inside a 60s connect budget. So 5s leaves ~2.5x margin on
#: the tightest real path and ~36x on qualification. #617's 10s drain is what keeps bars fast — it
#: holds requests below IB's pacing threshold, so it protects this bound rather than fighting it.
#:
#: DO NOT TIGHTEN TO 3s ON QUALIFICATION'S MARGIN. Bars at 1.97s observed would have 1.5x, and the
#: knob is shared. Any change to this number must be argued against the BAR distribution above.
#:
#: WHY IT MATTERS AT ALL: resolution is serial and an unresolvable contract pays the full wait (IB's
#: error 200 cancels the request with a no-op lambda and never resolves the future), while
#: `_connect()` qualifies everything BEFORE reporting connected — so busting the 60s budget makes
#: `kernel.py:1020-1025` skip `_trader.start()` and the node comes up with ZERO strategies.
#:
#: IT BUYS A MARGIN, NOT IMMUNITY: 17.2s healthy + five bad tickers at 5s = 42.2s of 60s. Around
#: eight bad tickers busts it again, so the margin is worth watching after a pool refresh (#716 is
#: the root cause — an unvalidated CSV keeps minting them).
#:
#: No ORDER path is bounded by this: place_order, place_order_list and cancel_order are synchronous
#: fire-and-forget with no `_await_request`, so this cannot make an order or a cancel fail. And the
#: reconciliation reads return None (never []) on failure — `order.py:161` and `account.py:174` say
#: "caller must not infer", and the callers leave state unchanged — so a slow answer cannot
#: manufacture the ORDER_NOT_FOUND_AT_VENUE rejections of #512.
_IB_REQUEST_TIMEOUT_SECS = 5


def _market_data_type(name: str):
    """`"DELAYED"` -> `IBMarketDataTypeEnum.DELAYED`. REFUSES a name the adapter does not know.

    Refuse, never default: a typo ("delayed", "Delayed", "DELAY") silently falling back to REALTIME
    would reproduce the exact silent-stream failure this key exists to escape, and it would do so
    while the instance file reads as if delayed data had been chosen — prose that reads as a decision
    beside a value that says the opposite (#574 family).
    """
    allowed = {a for a in dir(IBMarketDataTypeEnum) if a.isupper()}
    key = str(name).strip()
    if key not in allowed:
        raise ValueError(
            f"[data.ibkr] market_data_type={name!r} is not one the installed IB adapter knows; "
            f"allowed: {sorted(allowed)}. Refusing rather than defaulting to REALTIME."
        )
    return getattr(IBMarketDataTypeEnum, key)


def build_data(provider_config: dict) -> DataClientSpec:
    """Build the IBKR *data* client spec from the `[data.ibkr]` table.

    Real-time bars/quotes over the SAME already-running gateway the exec client uses, but on a DISTINCT
    IB API client id (data=2, exec=1) — two sockets need different ids or the gateway rejects with 326.
    Uses MIC venues (convert_exchange_to_mic_venue) so streamed bars carry the same TICKER.MIC ids as the
    reconciled positions. Requires the account's real-time market-data entitlement (IBKR Pro).
    """
    _ensure_info_sanitiser()  # #929: before any contract details can be parsed into Instrument.info
    ibg_host = os.environ.get("KUMO_IBG_HOST", provider_config.get("ibg_host", "127.0.0.1"))
    ibg_port = int(os.environ.get("KUMO_IBG_PORT", provider_config.get("ibg_port", 4002)))
    ibg_client_id = int(provider_config.get("ibg_client_id", 2))
    from api.providers.ibkr_shortable import build_plane as _build_shortable_plane
    from api.providers.ibkr_rth_daily import RthDailyIBDataClientFactory

    return DataClientSpec(
        client_id=IB,
        config=InteractiveBrokersDataClientConfig(
            ibg_host=ibg_host,
            ibg_port=ibg_port,
            request_timeout_secs=_IB_REQUEST_TIMEOUT_SECS,  # see the constant (#525)
            ibg_client_id=ibg_client_id,
            # A `[data.ibkr]` KEY, NOT A LITERAL (#834). This was hardcoded REALTIME, and REALTIME on
            # a paper gateway whose user has a live TWS session open is a stream that never arrives:
            # IB serves real-time market data to ONE session per user, and it refuses the second one
            # SILENTLY — no error code, nothing. Measured on ibkr-paper across the whole 2026-09-09
            # open: 112 x `reqHistoricalData(keepUpToDate=True)` for 1-MINUTE bars, all accepted,
            # 0 live bars delivered in 40 open-market minutes, while the same call for 1-DAY pushed
            # exactly one bar per symbol — yesterday's — stamped 00:00 UTC. That single stamp is
            # what the UI rendered as "feed 14h".
            #
            # DELAYED (IB type 3) is served WITHOUT the real-time entitlement, ~15 minutes behind.
            # Which is the right answer for this instance is an operator decision — a delayed print
            # is fine for a chart and wrong for an entry — so it is declared per instance and the
            # UI has to say which it is getting. Default stays REALTIME: inert everywhere unset.
            market_data_type=_market_data_type(provider_config.get("market_data_type", "REALTIME")),
            use_regular_trading_hours=False,  # include pre/post-market so prices move outside RTH
            handle_revised_bars=True,  # IB re-sends the forming bar as it updates
            # THE BAR REQUESTS COME FROM THIS CLIENT, so this is the provider that has to know the
            # contracts (#606). It carried NO `load_contracts` at all, so it resolved only what it
            # happened to be handed — and every compass request was refused:
            #   Cannot request XLU.ARCX-1-DAY-LAST-EXTERNAL bars: instrument not found   (21 of 27)
            # Wiring the union into the EXEC client alone would not have fixed it; the exec provider
            # is a different object and never sees a bar request.
            instrument_provider=InteractiveBrokersInstrumentProviderConfig(
                convert_exchange_to_mic_venue=True,
                # Checked BEFORE exchange resolution (providers.py:825-829), so this is the only way
                # to stop the adapter's silent raw-exchange fallback minting a non-MIC venue (#622).
                symbol_to_mic_venue=_symbol_mic_overrides(),
                load_contracts=_data_contracts(),
            ),
        ),
        # THE SHIPPED FACTORY, WRAPPED (#875): DAY bars are requested and subscribed REGULAR-HOURS on
        # the IB client it returns; every other bar type keeps `use_regular_trading_hours` above.
        factory=RthDailyIBDataClientFactory,
        # IB documents ~60 historical requests per 10 minutes. Declared here because it is IB's
        # limit, not the platform's — nothing above the connector knows this number exists (#617).
        historical_requests_per_minute=float(
            os.environ.get("KUMO_IBKR_HIST_REQ_PER_MIN", "6")),
        # IB's contract details carry liquidHours + timeZoneId (#628).
        supplies_trading_calendar=True,
        # RTH BECAUSE OF THE FACTORY ABOVE, AND ONLY BECAUSE OF IT (#875). The installed adapter
        # forwards `use_regular_trading_hours=False` into every bar request and subscription
        # (adapters/interactive_brokers/data.py:625, :283) — so on the vendor factory this node's
        # 1-day OHLC folds pre/post-market in (highs up to 14% off, #616 accepted that for the chart).
        # `RthDailyIBDataClientFactory` rewrites `use_rth` for DAY aggregations on the IB client, for
        # the WHOLE node, chart included. This declaration and that factory are one fact in two lines
        # (test_ibkr_rth_daily pins them together); change both or neither.
        daily_bars_cover="rth",
        # IB's TRADES daily bars are split-adjusted at request time and carry no adjustment
        # parameter — `split` is the ONLY series this client serves (measured: NVDA 2024-06-07 =
        # 120.89 on ibkr-paper's gateway, the number Alpaca gives under `adjustment=split`). A request
        # naming `raw` here is answered with split bars; the requester reads the declaration first.
        price_adjustments=frozenset({"split"}),
        # MEASURED, NOT ASSUMED (#612). IB serves market data to ONE session per user and the live
        # session holds it, so this paper gateway gets `10197: No market data during competing live
        # session` and no tape ever arrives — while exec works perfectly. Declaring False refuses the
        # INTERNAL granularities that would otherwise aggregate from a tape that never comes and sit
        # empty with no error, which is what blanked four sparklines and left 11 of 22 held positions
        # unpriceable on 2026-08-31.
        streams_trade_ticks=False,
        # IB shortability (#857): generic tick 236 → 46/89, published as a custom Data. Declared
        # HERE, inside the connector, so the engine holds a vendor-neutral plane and never an IB
        # import (test_import_boundary). Bound to the DATA client's factory key.
        shortable_plane=lambda publish, now_ns: _build_shortable_plane(
            publish, now_ns, host=ibg_host, port=ibg_port, client_id=ibg_client_id),
        # SAME ENTITLEMENT, SAME ANSWER, MEASURED SEPARATELY (#812). The quote plane reaches IB
        # through `reqTickByTickData` too, so it is refused the same way — ibkr-paper's boot of
        # 2026-09-09 subscribed 112 quote streams and got 135 x 10189 (no market data permissions
        # for NYSE / ISLAND / AMEX STK) and 199 x 10190 (max tick-by-tick requests reached), with
        # zero ticks delivered on either plane.
        #
        # DECLARED RATHER THAN INFERRED FROM `streams_trade_ticks`. They are two facts about a venue
        # and they happen to agree here; a provider that served one and not the other would be
        # mis-declared by any rule that derives one from the other.
        streams_quote_ticks=False,
    )


def build(exec_config: dict) -> ExecClientSpec:
    """Build the IBKR exec client spec from the `[execution.ibkr]` table.

    Connects to an already-running gateway at ibg_host:ibg_port (default the dockerized paper gw on 4002).
    The account id is read from the env var named by `account_id_env` (injected by run-api.sh), never
    committed.
    """
    account_id_env = exec_config["account_id_env"]
    account_id = os.environ.get(account_id_env)
    if not account_id:
        raise RuntimeError(
            f"{account_id_env} unset — launch via backend/scripts/run-api.sh (injects it from the keychain)."
        )
    # Env overrides (KUMO_IBG_HOST/PORT) take precedence over config — the container points them at the
    # `ib-gateway` compose service (#21); locally they fall back to the config's 127.0.0.1:4002.
    ibg_host = os.environ.get("KUMO_IBG_HOST", exec_config.get("ibg_host", "127.0.0.1"))
    ibg_port = int(os.environ.get("KUMO_IBG_PORT", exec_config.get("ibg_port", 4002)))
    config = InteractiveBrokersExecClientConfig(
        ibg_host=ibg_host,
        ibg_port=ibg_port,
        request_timeout_secs=_IB_REQUEST_TIMEOUT_SECS,  # see the constant (#525)
        ibg_client_id=int(exec_config.get("ibg_client_id", 1)),
        account_id=account_id,
        # MIC venues (AAMI.XNYS, INTC.XNAS) via Nautilus's built-in exchange→MIC table, so reconciled
        # positions share the data provider's canonical TICKER.MIC ids.
        #
        # `load_contracts` IS WHAT LETS THIS CLIENT TRADE ANYTHING IT DOES NOT ALREADY HOLD. Without
        # it the provider has no load directive, so the only instruments it ever holds are the ones
        # RECONCILIATION brings in with open positions. On 2026-08-24 ibkr-paper-retired had qualified
        # exactly 16 instruments — exactly its 16 open positions — and BCTROT-004's 8 entries all
        # died at submit with `AttributeError('NoneType' object has no attribute 'is_inverse')`,
        # which is the instrument lookup returning None. The lane could rank 98 names and submit 8.
        #
        # NOT `[universe]` — that was tried and measured: seeding all 98 pool symbols there changed
        # nothing, because `[universe]` drives the ALPACA DATA client and never reaches this one.
        # It is read here as the deployment's declared tradeable set, which is the same list, but it
        # has to arrive through THIS config to have any effect.
        #
        # SMART routing rather than a per-symbol `primaryExchange`: IB resolves unambiguous US
        # tickers on SMART, and a wrong primaryExchange is the same None lookup by another route.
        instrument_provider=InteractiveBrokersInstrumentProviderConfig(
            convert_exchange_to_mic_venue=True,
            # BOTH clients, deliberately. The data and exec providers are different objects, and #606
            # shipped a fix wired into one of them that changed nothing. A venue that differs between
            # them is a split identity — worse than the defect being fixed.
            symbol_to_mic_venue=_symbol_mic_overrides(),
            # TRADEABLE ONLY, DELIBERATELY. `load_contracts` here is the set this client may
            # TRADE, so the compass's reference ETFs must NOT appear: they are graded, never held,
            # and widening the tradeable surface to fix a display plane is the wrong trade. The
            # existing tripwire in `test_ibkr_instrument_universe.py` caught exactly that when the
            # first version of #606 put the union here — the DATA client is where bars are
            # requested, and that is where the reference set belongs.
            load_contracts=frozenset(
                IBContract(secType="STK", symbol=s, exchange="SMART", currency="USD")
                for s in _tradeable_symbols()
            ),
        ),
    )
    return ExecClientSpec(
        client_id=IB,
        config=config,
        # GATED, because the shipped adapter enforces no per-strategy budget and neither did we
        # (#782). BCTROT-004 was allocated 100,000 and reached 118,967.75 on this stack: `may_submit`
        # had one call site, in the ALPACA exec client, and staging never goes near it.
        factory=BudgetGatedIBExecClientFactory,
        # IB does not hold shares against a resting order the way Alpaca does (#430, INCIDENT
        # provenance in api/venues/facts.py) — a SELL beside a resting protective stop is accepted, so
        # the exit path's availability wait has nothing to wait for and must be skipped (#641).
        reserves_shares_against_resting_stop=False,
    )


class BudgetGatedIBExecClientFactory(InteractiveBrokersLiveExecClientFactory):
    """The shipped IBKR factory, with this repo's two wraps installed on what it returns.

    THE VENDOR STILL OWNS CONSTRUCTION. `create` delegates and then wraps; nothing about the
    client's construction is reproduced here, because copying a vendor constructor into our tree is
    how an adapter breaks silently on the next bump.

    The gate itself is `api.budget_guard.budget_allows` — the SAME function the Alpaca client calls.
    Two exec clients running two copies of a capital rule would drift, and that drift is #782.
    """

    #: A MARKER, checked by `test_every_exec_client_is_gated`. The guard used to grep the module
    #: source for "install_budget_gate", which a dead class definition left behind satisfied — a scan
    #: its own error message taught how to evade. Resolving the spec's factory and reading this
    #: cannot be fooled by unused code.
    gates_budget = True

    #: The same kind of marker for #785. It is NOT the test that matters, though: a marker set on a
    #: `create` that forgot to install anything is exactly the "declared but not connected" shape,
    #: so `test_the_REPO_FACTORY_returns_a_client_whose_BATCH_SURVIVES_a_refless_order` drives this
    #: factory for real and asks the returned object.
    filters_refless_orders = True
    #: #1030. The ownership guard (#748) lived inline in the Alpaca client and never reached this
    #: one — so the venue the operator trades by hand had no guard against the WHD shape. Same marker
    #: discipline as the two above; `test_ownership_gate_both_venues` DRIVES `create` as well.
    gates_ownership = True

    @staticmethod
    def create(loop, name, config, msgbus, cache, clock):
        from api.budget_journal import journal_row
        from api.providers.gated_exec import install_budget_gate, install_ownership_gate
        from api.providers.ib_refless_orders import install_refless_order_filter

        client = InteractiveBrokersLiveExecClientFactory.create(
            loop=loop, name=name, config=config, msgbus=msgbus, cache=cache, clock=clock,
        )
        # WRAPS ON ONE INSTANCE, and they touch different methods: the budget gate wraps the
        # exec client's own `_submit_order` (what leaves for the venue), the refless filter wraps
        # `_client.get_open_orders` (what arrives from it). Order does not matter; both must be here,
        # and `install_budget_gate` returning the client is what lets them compose in one line.
        # #990: the journal writer is what makes an ALLOW visible after the container is gone.
        # THREE WRAPS. Ownership is installed AFTER budget so it runs BEFORE it (the last wrap
        # installed is the outermost): a budget verdict on a mis-stamped exit answers the wrong
        # question. `install_budget_gate` refuses the other order at install time.
        return install_refless_order_filter(
            install_ownership_gate(install_budget_gate(client, journal=journal_row)))


def _reference_symbols() -> tuple[str, ...]:
    """The market compass's grading universe — reference instruments, never held (#606).

    NOT TRADEABLE, WHICH IS WHY THEY WERE MISSING. `load_contracts` was built from
    `_tradeable_symbols()` alone, so SPY, TLT, GLD and the sector XLs were never declared to the
    IBKR instrument provider, never loaded, and every bar request for them came back "instrument not
    found". Measured on an IBKR paper instance 2026-08-27: 0 of 27 compass tickers were in the tradeable universe,
    21 of 27 bar requests refused, and `rotation: seeding, 27 of 27 instruments still short` every
    five minutes for the whole session with ZERO axes published.

    The venue was never the limitation. IBKR serves all of these; we did not ask for the contracts.

    BEST EFFORT, because the compass is a display plane and the exec client is not. If the rotation
    universe cannot be read, execution must still come up — an operator losing the market tab is a
    far smaller failure than a node that will not trade, and this module's whole purpose is the
    latter.
    """
    try:
        from strategies.rotation_from_cache import rotation_tickers

        return tuple(rotation_tickers())
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "IBKR: could not read the rotation reference universe (%r) — the compass will have no "
            "history on this node, but execution is unaffected", exc,
        )
        return ()


def _data_contracts() -> frozenset:
    """Every contract the DATA client must resolve: what we trade plus what we grade with.

    NOT the exec client's set. That one defines what may be TRADED and must stay the tradeable
    universe alone — reference ETFs are graded, never held.

    A UNION, so an overlap is declared once. IB bills a market-data line per contract and a
    duplicate is a wasted one — `SPY` being both tradeable and a compass axis must not cost two.

    The tradeable set still raises when empty. Adding reference contracts must not make that error
    unreachable: a deployment with no universe can trade only what reconciliation drags in, which is
    the outage `_tradeable_symbols` exists to refuse.
    """
    # SUBTRACTED AFTER THE UNION, or the reference set is a second door into the same denylist.
    # The two are disjoint today; that is a fact about today's rotation universe, not a property of
    # the code, and #511's whole lesson is that a union cannot express "absent on purpose".
    excluded = {_norm(x) for x in _excluded_symbols()}
    symbols = sorted(({_norm(x) for x in _tradeable_symbols()}
                      | {_norm(x) for x in _reference_symbols()}) - excluded)
    return frozenset(
        IBContract(secType="STK", symbol=s, exchange="SMART", currency="USD") for s in symbols
    )


def _norm(symbol: str) -> str:
    """One spelling rule for every set that meets another. Declared once because the exclusion, the
    declared universe and the pool are compared against each other, and three private conventions
    are three chances for a denylist to be defeated by whitespace."""
    return str(symbol).strip().upper()


def _tradeable_symbols() -> tuple[str, ...]:
    """The deployment's declared tradeable set, for `load_contracts`.

    RAISES ON EMPTY, deliberately. An empty `load_contracts` is not "load nothing on purpose" — it is
    the pre-2026-08-24 broken state wearing a directive, and it fails invisibly at submit time rather
    than at boot. `load_feed_config` already refuses a config with no universe symbols; this keeps
    that guarantee at the point of use, because a caller that quietly built an empty frozenset would
    reproduce the outage with no error anywhere.

    Read from the feed config directly rather than threaded through `build_exec_client_spec`: the
    registry's builder contract is `(config) -> Spec` and every other provider shares it, so widening
    the signature to pass one provider's universe would change a contract three builders honour to
    serve one. Same file, same deployment, one extra parse at construction.
    """
    declared = _declared_symbols()
    try:
        pooled = _pool_symbols()
    except Exception as exc:  # noqa: BLE001
        # A FAILED READ IS NOT AN EMPTY POOL. Shrinking what the lane may trade because Postgres
        # hiccuped would take away exactly the names it is holding, so the declared universe stands
        # and the degradation is REPORTED rather than passing as a normal boot.
        _log.error(
            "IBKR tradeable set: the symbol pool could not be read (%r) — falling back to the "
            "declared universe of %d symbol(s). Names added to the pool since the last deploy "
            "cannot be traded this session.", exc, len(declared))
        pooled = ()

    # EXCLUSION IS A STATE, NOT AN ABSENCE (#511 review). `feed.toml` used to exclude the five
    # unresolvable tickers by leaving them OUT of `[universe]`, with a comment saying that is what
    # keeps the boot inside its connect budget — and a union with the pool CANNOT EXPRESS "absent on
    # purpose". The pool carries all five, so the union put BLLLN, FTNR, IQVIA, JEPO and OVVI
    # straight back into both clients' `load_contracts` (exec 93->119, data 114->140).
    #
    # MEASURED against staging's gateway on 2026-08-29 (scratch probe driving this adapter's own
    # `get_contract_details`, client id 9), because what IB does with a symbol is not knowable by
    # inspection:
    #     the 21 symbols the pool ADDS       9-75ms each, 0.6s for all 21
    #     BLLLN FTNR IQVIA JEPO OVVI         60,003ms EACH — a hard hang, five of them
    #     BLLN 18ms, IQV 52ms                the correct spellings resolve instantly (#716)
    # The node's whole startup budget is 60s. Unbounded, any ONE of the five is a zero-strategy
    # boot on its own; bounded by #525's `request_timeout_secs=5` it is five 5s stalls inside the
    # provider's serial loop, ~25s of that budget spent on tickers that do not exist. The pool
    # union itself costs 0.6s.
    #
    # NOT `exec_pool_override` with kind='exclude': that marks a symbol for LIQUIDATION at the next
    # session (app.py:1223), which is the wrong instrument for "this ticker does not exist".
    # ALL THREE NORMALIZED, not just the exclusions. The pool is not guaranteed uppercase: the
    # validating door forwards `body.symbols` verbatim whenever nothing was refused (app.py:1186),
    # and on an instance with no catalog nothing ever IS refused (#663) — while ledger.py writes
    # `str(r.symbol)` straight from the same unvalidated CSV that minted BLLLN. A lowercase or
    # padded row would slip past an uppercase-only denylist and mint the junk contract anyway. Two
    # derivations of one identity that must not drift.
    lanes = _lane_universe_symbols()
    excluded = {_norm(s) for s in _excluded_symbols()}
    symbols = tuple(sorted(({_norm(s) for s in declared} | {_norm(s) for s in pooled}
                            | {_norm(s) for s in lanes}) - excluded))
    if not symbols:
        raise RuntimeError(
            "IBKR execution is configured but neither the pool nor the feed config yields any "
            "universe symbols, so the exec client would be able to trade ONLY instruments "
            "reconciliation happens to bring in with open positions — every new entry would fail "
            f"at submit with a None instrument. ({len(excluded)} symbol(s) are explicitly "
            f"excluded; check that the exclusion list has not swallowed the universe.)")
    if pooled:
        # NORMALIZED, like the set it describes. Computing this on raw values makes the log
        # disagree with the tradeable set exactly when a non-uppercase row appears — which is the
        # case `_norm` exists for, so the one line that would explain it would be the wrong one.
        added = sorted({_norm(x) for x in pooled} - {_norm(x) for x in declared})
        if added:
            _log.info("IBKR tradeable set: %d symbol(s) from the pool that the declared universe "
                      "does not carry: %s", len(added), ", ".join(added[:20]))
    return symbols


def _lane_universe_symbols() -> tuple[str, ...]:
    """Every ENABLED lane's settings universe (#871) — QC27_UNIVERSE, QC345_UNIVERSE, CRSI_UNIVERSE —
    through the ONE table in `api.lane_universes`, which the lanes' own readers also use.

    Measured on ibkr-paper 2026-09-10: TECHIVOL-005 "125 of 138 symbols have no instrument on this
    venue" at boot, because these names were never in `load_contracts`. IB lists every one of them;
    we did not ask. Same failure as #511 (the pool) and #606 (the compass), one plane over.

    BEST EFFORT, like the reference set: an unreadable settings domain must not stop the node
    booting, and it says so at ERROR with the count that is missing — a lane that then boots with
    a narrower universe reports it in its own boot line.
    """
    try:
        from api import settings
        from api.lane_universes import enabled_lane_universe_symbols

        by_lane = enabled_lane_universe_symbols(settings.resolve("strategies") or {})
    except Exception as exc:  # noqa: BLE001
        _log.error("IBKR tradeable set: lane universes unreadable (%r) — enabled lanes will resolve "
                   "only the names the feed config and the pool carry", exc)
        return ()
    out: set[str] = set()
    for key, syms in by_lane.items():
        if syms:
            _log.info("IBKR tradeable set: %d symbol(s) from %s", len(syms), key)
        out.update(syms)
    return tuple(sorted(out))


def _excluded_symbols() -> tuple[str, ...]:
    """Symbols this instance declares unusable — `[universe] exclude` in its own feed.toml.

    A DECLARED STATE, so the next union cannot forget it. The five live entries are unresolvable at
    IB (three of them transcription errors: BLLN/BLLLN, FTNT/FTNR, IQV/IQVIA), and each one costs a
    full per-request timeout on BOTH clients inside the connect budget.

    Deliberately not the claims/override table: an exclude row there means LIQUIDATE at the next
    session, which is a trading instruction, not a statement about whether a ticker exists.
    """
    from api.feed_config import load_feed_config

    # NO try/except. "The exclusion list could not be read" is not "nothing is excluded" — that
    # conversion turns a failed read into permission to load five contracts that hang the connect
    # for 60s each. `_declared_symbols` reads the same file through the same loader and raises, so a
    # swallow here could never even fire; its only reachable effect would be the dangerous one.
    return tuple(load_feed_config().excluded_symbols)


def _declared_symbols() -> tuple[str, ...]:
    """The deployment's declared universe from `feed.toml` — the operator's floor."""
    from api.feed_config import load_feed_config

    return tuple(load_feed_config().symbols)


#: ONE POOL READ PER PROCESS, and the exec and data clients share its answer. Not an optimisation:
#: `build_node` calls `_tradeable_symbols` twice — once for the exec client's `load_contracts` and
#: once inside `_data_contracts` — and two independent reads CAN DISAGREE. A transient failure on
#: the second returns the declared universe alone, so a pool-added name would be tradeable on exec
#: while the data client refuses its bars as "instrument not found" (#606's exact symptom, now
#: intermittent and boot-dependent). A split universe between two clients of one node is the
#: condition `symbol_to_mic_venue` is applied to both clients to avoid; this is the same class.
#:
#: THE OUTCOME IS CACHED, NOT THE SUCCESS. Caching only successful reads left the exact split this
#: exists to prevent: a first read that fails gives the EXEC client the declared universe, then
#: `_data_contracts` retries and the second read succeeds, so the DATA client gets declared|pool.
#: Direction matters — data a superset of exec means a lane sees bars for AMZN, ranks it, and the
#: submit dies on the None-instrument AttributeError, which is the 2026-08-24 outage back as an
#: intermittent, boot-dependent one. A failed read is therefore remembered and re-raised, so one
#: boot gives ONE answer to both clients whichever way it went.
#:
#: Process-lifetime, because a builder runs once per boot and a restart is how the pool is picked up.
_POOL_CACHE: tuple[str, ...] | Exception | None = None

#: Bounds the pool read on the connect path. A module constant so a test can drive the timeout
#: rather than sleep through it.
_POOL_READ_TIMEOUT_SECS = 5


def _pool_symbols() -> tuple[str, ...]:
    """Every symbol the SYMBOL POOL currently holds (#511).

    `feed.toml`'s `[universe]` is a snapshot and says so — "the POOL as it stood 2026-08-24 ... it
    does not refresh with the pool". Measured on an IBKR paper instance 2026-08-29: pool 106, snapshot 93, and
    twenty ordinary listable names (AMZN, CRWD, FTNT, JAZZ, ...) rankable but unsubmittable,
    refused at the broker with "not a subscribed instrument" before an order is built.

    SYNCHRONOUS, because this runs inside the client builder at construction, before any event loop
    the async pool store could use. One SELECT against the same Postgres the lanes write.
    """
    import asyncio

    import asyncpg

    from api.db.engine import database_url

    async def _read() -> tuple[str, ...]:
        # ONE BOUND, ONE SPELLING. The connect and the fetch are two halves of the same budget and
        # a literal beside a constant is how they drift.
        conn = await asyncpg.connect(database_url().replace("postgresql+asyncpg://", "postgresql://"),
                                     timeout=_POOL_READ_TIMEOUT_SECS)
        try:
            # BOUNDED, like the connect beside it. A Postgres that ACCEPTS and then stalls
            # would hang `build_node` forever — before the node's 60s budget starts and before
            # the inert-node watchdog exists, so nothing downstream could time it out. This whole
            # change is about not spending the connect path on an unbounded wait (#525).
            rows = await asyncio.wait_for(
                conn.fetch("SELECT DISTINCT symbol FROM exec_pool_source"),
                timeout=_POOL_READ_TIMEOUT_SECS)
        finally:
            # BOUNDED TOO. asyncpg's graceful close waits on the same peer that just failed to
            # answer, so an unbounded close reintroduces the hang one line below the fix for it.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(conn.close(), timeout=2)
        return tuple(sorted(r["symbol"] for r in rows if r["symbol"]))

    global _POOL_CACHE
    if isinstance(_POOL_CACHE, Exception):
        raise _POOL_CACHE                    # this boot already asked and already failed
    if _POOL_CACHE is not None:
        return _POOL_CACHE

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            _POOL_CACHE = asyncio.run(_read())   # the normal path: builders run before the node loop
        except Exception as exc:
            _POOL_CACHE = exc
            raise
        return _POOL_CACHE
    # A LOOP IS ALREADY RUNNING, so `asyncio.run` would raise. Rather than block it with a nested
    # loop, decline and let the caller fall back to the declared universe — loudly, because a
    # silently smaller tradeable set is the defect this function exists to fix.
    _POOL_CACHE = RuntimeError(
        "the symbol pool cannot be read from inside a running event loop; the declared universe "
        "stands for this boot")
    raise _POOL_CACHE


# --- #929: ibapi value objects must not reach Instrument.info -------------------------------------------
#
# MEASURED 2026-09-11: IB attached `ineligibilityReasonList=[IneligibilityReason(i152, "Instrument
# temporarily unavailable pending configuration review.")]` to OKE across the day roll. The adapter copies
# `contract_details.dict()` into `Instrument.info` (`parsing/instruments.py:1000 contract_details_to_dict`)
# and its `_serialize_for_json` converts Decimal and Enum only, so the ibapi object survives; the durable
# cache's msgspec serializer has no hook for it and raises inside `DataEngine._handle_instrument`, and the
# live DataEngine TERMINATES THE NODE. Six restarts on an unchanged image. Boot-blocking for any IBKR
# instance the moment IB flags any instrument in its universe.
#
# NOT A FORK. `parse_*_contract` in the adapter call `contract_details_to_dict` through their module's
# globals, so rebinding that one name at import time is honoured at call time. The wrapper converts any
# ibapi value object (anything whose class lives in the `ibapi` package) into a plain dict, recursively,
# and leaves everything else exactly as the adapter produced it. Remove when nautilus_trader's
# `_serialize_for_json` handles ibapi objects (upstream ticket in #929).

def _plain(obj):
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if type(obj).__module__.startswith("ibapi") and hasattr(obj, "__dict__"):
        return {k.rstrip("_"): _plain(v) for k, v in vars(obj).items()}
    return obj


def install_info_sanitiser() -> None:
    """Wrap the adapter's `contract_details_to_dict` once so ibapi value objects become dicts (#929)."""
    from nautilus_trader.adapters.interactive_brokers.parsing import instruments as parsing
    current = parsing.contract_details_to_dict
    if getattr(current, "_kumo_sanitised", False):
        return

    def sanitised(contract_details):
        return _plain(current(contract_details))

    sanitised._kumo_sanitised = True          # type: ignore[attr-defined]
    sanitised.__wrapped__ = current           # type: ignore[attr-defined]
    parsing.contract_details_to_dict = sanitised
    _log.info("IBKR: instrument info sanitiser installed (#929) — ibapi value objects become dicts before the cache")


def _ensure_info_sanitiser() -> None:
    """The ONE place the connector guarantees the wrapper before any instrument can be parsed."""
    install_info_sanitiser()
