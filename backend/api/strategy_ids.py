"""Cockpit strategy identities (#68/#72, Phase-2 Step 0).

The `strategy_id` IS the cycle-attribution key: in NETTING mode Nautilus builds the position id as
`{instrument}-{strategy_id}`, so a strategy's id must be a STABLE, meaningful contract — NOT the
class-name-derived default (`UiFeedStrategy-000`), which codex flagged as unacceptable cycle identity.

Format is Nautilus `StrategyId("NAME-tag")`. The cockpit STRATEGY (MANUAL / MOMENTUM / ETF_AUTO) is the NAME
part; `strategy_name()` recovers it. Do not rename these — positions and trade cycles key off them.
"""

from __future__ import annotations

from nautilus_trader.model.identifiers import StrategyId

# The always-live discretionary strategy. Owns the human's MANUAL orders (round-1 = the only strategy).
# A StrategyId is `NAME-tag`. We keep the parts because a strategy's id is built by Nautilus from its config
# as `f"{strategy_id}-{order_id_tag}"`: passing name=`MANUAL` + tag=`001` yields exactly `MANUAL-001` with the
# order-id tag `001` bound at registration — so client_order_id / order-list / position ids all stay aligned
# via `id.get_tag()`. (Setting the id another way, e.g. change_id(), would leave the tag at the config default
# and desync client_order_id generation from the id — do NOT do that.)
MANUAL_NAME = "MANUAL"
MANUAL_TAG = "001"
MANUAL = StrategyId(f"{MANUAL_NAME}-{MANUAL_TAG}")  # "MANUAL-001"

# The ETF-universe rotation strategy (#8). IDENTITY ONLY at this point — nothing constructs a Nautilus
# strategy with it yet, and it is deliberately NOT wired into order submission. It exists so the evidence
# produced out-of-tree in `kumo-strategies` (backtest artifacts, decision/order/fill tables) can carry the
# cockpit's real `strategy_id` from the first run, rather than being re-keyed at integration time — a
# re-key would orphan every artifact against the `(account_id, client_id, instrument_id, strategy_id,
# cycle_id)` contract.
#
# `GEM-VT` / `gem_vt` is the MODEL/CONFIG provenance of the rotation rules, NOT a strategy id. It belongs
# in an artifact's config/provenance fields; the persisted strategy field is always `ETF_AUTO-001`.
# Nautilus requires `order_id_tag` UNIQUE across every strategy registered in one trader — the tag is
# the StrategyId suffix and seeds client-order-id generation. Giving them all "001" works only while
# exactly one is registered; the second fails at add_strategy with "order_id_tag conflict". So the tag
# is allocated per strategy, not per instance. MANUAL keeps 001 (it has live positions keyed to it).
#
# ETF_AUTO ALSO still holds 001 and is deliberately left there: out-of-tree backtest artifacts already
# carry `ETF_AUTO-001` in their strategy_id, so moving it orphans them (test_strategy_ids.py). It does
# not conflict today because ETF_AUTO is not registered in the node. When it is wired it must take a
# free tag AND its artifacts must be migrated — that is a deliberate piece of work, not a rename.
MOMENTUM_NAME = "MOMENTUM"
MOMENTUM_TAG = "002"
MOMENTUM = StrategyId(f"{MOMENTUM_NAME}-{MOMENTUM_TAG}")  # "MOMENTUM-002"
ETF_AUTO_NAME = "ETF_AUTO"
ETF_AUTO_TAG = "001"
ETF_AUTO = StrategyId(f"{ETF_AUTO_NAME}-{ETF_AUTO_TAG}")  # "ETF_AUTO-001"


# QC27 Tech Momentum with Inverse Volatility (kumo-strategies#33, #63). The wire id is the STRATEGY
# NAME, not the QC leaderboard number — `QC27` stays provenance and lives in the adapter's
# `EXTERNAL_ID`. Tag 005 because 001-004 are MANUAL, MOMENTUM, QC345 and BCTROT; the QC27 adapter
# deliberately has NO default tag and raises without one, so this allocation is explicit rather than
# a default that happens to agree. A duplicate does not degrade: Nautilus raises at
# `Trader.add_strategy` and the node does not boot, taking every other strategy with it.
#
# NOTE this file is not the full registry — QC345-003 and BCTROT-004 declare their tags in their own
# modules. Do not read it as the authority on which tags are free.
TECHIVOL_NAME = "TECHIVOL"
TECHIVOL_TAG = "005"
TECHIVOL = StrategyId(f"{TECHIVOL_NAME}-{TECHIVOL_TAG}")  # "TECHIVOL-005"


def strategy_name(strategy_id: StrategyId | str) -> str:
    """The cockpit strategy NAME from a StrategyId `NAME-tag` (e.g. `MANUAL-001` → `MANUAL`). The name is the
    stable cockpit-facing identity; the tag is a per-instance suffix Nautilus requires."""
    return str(strategy_id).rsplit("-", 1)[0]
