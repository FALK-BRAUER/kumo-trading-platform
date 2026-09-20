"""Typed reader for the market-data feed config (`config/feed.toml`).

Config is data, not code, so the provider/universe/window change without a redeploy. The selected
provider's table (`[data.<provider>]`) is passed through verbatim to the provider registry, so adding
a provider needs no change here.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


# backend/api/feed_config.py → backend/config/feed.toml (cwd-independent).
#
# `KUMO_FEED_CONFIG` OVERRIDES IT, so an INSTANCE owns this file rather than the platform (#486).
# feed.toml selects the data provider, the execution provider, the trader id, the venue and the traded
# UNIVERSE — every one of those is a property of a deployment, not of the software, and committing them
# to the platform is what made `kumo-trading-platform` un-runnable by anyone else.
#
# The default is UNCHANGED, deliberately: with the variable unset this resolves exactly where it always
# did, so the platform still runs standalone and nothing that exists today has to move first.
def _feed_config_path() -> Path:
    """Where feed.toml lives: the instance's, or the platform's own.

    A FUNCTION rather than a module constant so it can be tested without `importlib.reload`. Reloading
    a module to test one value re-runs every import side effect it has — which broke 34 unrelated tests
    the first time this was written, none of them about feed config.
    """
    return Path(
        os.environ.get("KUMO_FEED_CONFIG")
        or Path(__file__).resolve().parent.parent / "config" / "feed.toml"
    )


_CONFIG_PATH = _feed_config_path()

# UI-bridge latest-state planes (#56). ONE definition shared by the producer (engine_node) and consumer:
# these kinds go to Redis KEYS (SET/overwrite, TTL) instead of the bounded bar stream, so their 2s snapshot
# cadence can't evict the low-volume historical bars. If the two ever disagreed, a kind would land on the
# stream (flood returns) or a key nobody polls (plane vanishes) — hence a single source of truth.
#: `session` (#212) carries the STRATEGY's own state — lifecycle, the latest decision with its
#: per-symbol reasons, recent journal rows and the exit trail. Everything else here describes the
#: ACCOUNT; without this the cockpit can show what is held but never why, and four of the six
#: homescreen blocks have no data path at all.
#: `equity_curve` (#243) is the ACCOUNT's own history from the broker, per period. A latest-state plane
#: rather than a stream: only the newest snapshot matters, it refreshes every 2 minutes, and each frame
#: carries every period — putting that on the bounded bar stream would evict real history.
STATE_KINDS = frozenset({"positions", "orders", "account", "health", "trades", "external_activity",
                         "session", "equity_curve"})
STATE_KEY_PREFIX = "ui:state:"

#: Kinds that carry BAR history and must never share a stream with the quote/price flood (#309).
#:
#: Measured 2026-08-14: `ui:stream` held 200,015 entries against a 200,000 cap and retained 7.5 MINUTES.
#: Quotes and prices for ~119 symbols push ~27,000 entries a minute; a ~127,000-frame backfill is
#: therefore evicted within minutes, so a browser connecting afterwards never sees any history. The
#: watchlist sat on "loading…" with every 1M/1Y/5Y window showing "—" while the data had been fetched,
#: published, and thrown away.
#:
#: A separate stream means the two cannot compete: bars are written once and read whenever a client
#: arrives, at a rate that cannot evict them.
BAR_KINDS = frozenset({"bar"})


@dataclass(frozen=True)
class FeedConfig:
    """Resolved feed config: which provider, its raw table, the universe, and chart defaults."""

    data_provider: str
    provider_config: dict  # the [data.<provider>] table, handed to the provider registry
    backfill_days: int
    engine_kind: str  # "live" | "synthetic" — which NodeManager the app builds
    trader_id: str
    venue: str
    symbols: tuple[str, ...]
    chart_defaults: dict[str, str]  # chart range → default candle granularity
    chart_lookback_days: dict[str, int]  # candle granularity → history days to load
    exec_provider: str  # "ibkr" | "none"
    exec_config: dict  # the [execution.<provider>] table
    ui_bridge: dict  # the [ui_bridge] table (engine→Redis→UI feed, #20); empty/disabled → {}
    #: Symbols this instance declares UNUSABLE at its venue (#511). Excluded from `load_contracts`
    #: even when the symbol pool carries them: exclusion must be a STATE, because a union cannot
    #: express "absent on purpose" and the next pool refresh would put them straight back.
    excluded_symbols: tuple[str, ...] = ()


def _env_provider(var: str) -> str:
    """A provider name from the environment, or "" meaning THE OPERATOR DID NOT CHOOSE (#581).

    compose interpolates an UNSET variable to the EMPTY STRING — `KUMO_DATA: "${KUMO_DATA:-}"` — so
    `os.environ.get(var, default)` returns "" and the default never fires. That is the DEFAULT path for
    every instance that does not override, not an edge case.

    ONE helper for both knobs deliberately: `KUMO_DATA` and `KUMO_EXEC` are the same idea, and the
    asymmetry between them is what #574 and #581 were both about. Two derivations of one rule drift.
    """
    return os.environ.get(var, "").strip().lower()


def load_feed_config(path: Path = _CONFIG_PATH) -> FeedConfig:
    """Read and validate feed.toml."""
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    data = raw["data"]
    # KUMO_DATA SELECTS THE DATA PROVIDER, exactly as KUMO_EXEC selects the execution one (#574).
    #
    # It did not, until 2026-08-27. `instance.env` declared `KUMO_DATA` for both instances and the
    # instances README lists that file as holding "providers" — while this function read `feed.toml`
    # alone. Setting `KUMO_DATA=ibkr` changed nothing. Two sibling knobs, one wired, and it stayed
    # invisible because both sources said `alpaca` and AGREED: the exact condition under which a
    # severed wire cannot be observed.
    #
    # The cost was not cosmetic. ibkr-paper-retired took Alpaca data, which built an `AlpacaHttpClient`,
    # which the realized-P&L sweep then used to poll an Alpaca BROKERAGE account holding none of that
    # instance's positions (#573).
    # AN EMPTY STRING IS NOT A SELECTION (#581). compose interpolates an UNSET variable to the EMPTY
    # STRING — `KUMO_DATA: "${KUMO_DATA:-}"` — so `os.environ.get(k, default)` returns "" and the
    # default never fires. That is the DEFAULT path for every instance that does not override, not an
    # edge case, so it is handled explicitly rather than by a falsy `or` on the whole expression.
    provider = _env_provider("KUMO_DATA") or data["provider"]
    provider_config = data.get(provider)
    if not provider_config:
        # RAISES rather than falling back to the file's value. `KUMO_DATA=ibrk` must refuse, not run on
        # Alpaca while the operator believes otherwise — a fallback here is a silent wrong answer.
        raise ValueError(f"feed config selects provider {provider!r} but has no [data.{provider}] table")

    symbols = tuple(raw["universe"]["symbols"])
    # EXCLUSIONS ARE DECLARED, not encoded as absence (#511). A name here is unusable at this
    # instance's venue — unresolvable, a known misread — and must stay out of `load_contracts` even
    # when the symbol pool carries it, because each unresolvable contract costs a per-request
    # timeout inside the 60s connect budget on both clients.
    excluded_symbols = tuple(raw["universe"].get("exclude", ()))
    if not symbols:
        raise ValueError("feed config has no universe symbols")

    execution = raw.get("execution", {})
    # SAME RULE AS KUMO_DATA, and it became load-bearing the moment #581 forwarded this variable:
    # compose sets it to "" when unset, which resolved to exec_provider='' — not "none", not the
    # file's value — and `execution.get('', {})` gave the registry an empty config it cannot build.
    # Adding the passthrough for one knob created the identical defect on its sibling.
    exec_provider = _env_provider("KUMO_EXEC") or execution.get("provider", "none").strip().lower()
    # SAME RULE AS THE DATA PATH (#650): no table, no provider. `execution.get(provider, {})` was
    # the silent half — for alpaca an empty exec config builds a default PAPER client off whatever
    # APCA_* the environment carries, on an instance whose file never declared Alpaca execution.
    # On the path whose consequence is ORDERS, a fallback is a silent wrong answer.
    if exec_provider != "none":
        exec_config = execution.get(exec_provider)
        if not exec_config:
            raise ValueError(
                f"feed config selects execution provider {exec_provider!r} but has no "
                f"[execution.{exec_provider}] table")
    else:
        exec_config = {}

    ui_bridge = raw.get("ui_bridge", {})
    engine = raw["engine"]
    return FeedConfig(
        data_provider=provider,
        provider_config=provider_config,
        backfill_days=int(data["backfill_days"]),
        engine_kind=engine.get("kind", "live"),
        trader_id=engine["trader_id"],
        venue=engine["venue"],
        symbols=symbols,
        excluded_symbols=excluded_symbols,
        chart_defaults=dict(raw.get("chart", {}).get("defaults", {})),
        chart_lookback_days={k: int(v) for k, v in raw.get("chart", {}).get("lookback_days", {}).items()},
        exec_provider=exec_provider,
        exec_config=exec_config,
        ui_bridge=dict(ui_bridge),
    )
