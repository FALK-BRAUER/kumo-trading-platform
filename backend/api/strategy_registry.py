"""The declared set of strategies this node runs (#318).

Adding a strategy currently takes six hand-edits: an id in `strategy_ids.py`, a bespoke assembly
module, a hardcoded call in `engine_node.py`, its own env gate, a Postgres lifecycle, and — if anyone
remembers — a settings domain. A third strategy means a third copy of all six, which is the point at
which precedent should become a path.

COCKPIT OWNS THE INTERNAL ID. THE STRATEGY PROVIDES AN EXTERNAL ID AND A LABEL.
-------------------------------------------------------------------------------
2026-08-15: "why would you even look at the adapter. you just need any name if it contains 003
or whatever doesn't matter if you manage it at 006 ... the adapter should provide an external id and
label. you manage the internal id using the external id for mapping."

That is the fix, and my mistake was subtler than picking a wrong number. I read `STRATEGY_TAG = "003"`
out of the shipped adapter and reconciled cockpit TO it — which makes the adapter the authority on an
id cockpit is supposed to own, and turns every upstream rename into a cockpit migration.

The adapter's own name is ITS name. `external_id` is the stable key it is known by upstream; `label`
is what an operator reads. Cockpit assigns the internal `NAME-tag` and maps. Whether upstream calls
itself QC345, QC345-003 or something else stops being cockpit's problem, and two repos can never
disagree about a number again because only one of them holds it.

The mapping is what makes this safe rather than merely tidy: the internal id keys positions
(`{instrument}-{strategy_id}` under NETTING) and must never move once anything has traded. An external
id can change upstream without touching a single position, because it is only ever a lookup key.


2026-08-15, after a collision: "maybe the strategy # should be managed by cockpit and prefix by
strategy?"

Yes, and the collision is the argument. QC345 was assigned tag `003` by taking the next free number
here, while issue 32 had already published a strategy called `BCTROT-003`. Both were
reasonable in isolation. The tag is a GLOBAL SEQUENCE that two repositories have to agree on, and
nothing made them agree — so the first symptom would have been a node that would not boot.

The fix is ownership, not vigilance. Cockpit is the only place that sees every strategy in ONE TRADER
— it is the thing that calls `Trader.add_strategy` — so it allocates. Upstream names the STRATEGY
(`BCTROT`, `QC345`); it must not name the tag, because it cannot know what else is registered.

`_IMMOVABLE` is the durable record of that allocation. A tag lands there when it becomes unmovable:
because live positions are keyed to it, or because a name was published elsewhere before this rule
existed. "Nobody holds it yet" is NOT the same as "free" — that distinction is exactly what was missed.

WHY THIS IS NOT JUST A LIST
---------------------------
`order_id_tag` must be UNIQUE across every strategy in one trader. It is the suffix of the StrategyId
AND of every generated client order id, and Nautilus enforces it at `Trader.add_strategy` — so a
collision does not degrade, it stops the node booting:

    order_id_tag conflict for '001'

The cockpit convention of giving every strategy tag `001` worked only while exactly one was
registered. MANUAL holds `001` and has live positions keyed to `MANUAL-001`, so it can never move;
MOMENTUM took `002`; QC345 must take `003`. That is a rule nobody should have to remember, so the
registry ALLOCATES and validates tags rather than trusting the next author to notice.

`external_order_claims` are exclusive the same way and fail in the same place. Two strategies claiming
one instrument raises `InvalidConfiguration` during `add_strategy` — and QC345 shares MOMENTUM's
universe by construction, so this is a live hazard rather than a theoretical one. The registry is
where that overlap can be seen, because it is the only place that knows about all of them at once.

ENABLEMENT IS NOT DECLARED HERE, AND NOT IN THE ENVIRONMENT
-----------------------------------------------------------
This module records WHICH strategies exist. Whether one may act is DATABASE state — the lifecycle
machine (DISABLED -> WARMUP -> SHADOW -> TRADING, operator-gated at the last step) plus its sleeve row.

An earlier version of this file carried an `enabled_env` per strategy, copying MOMENTUM's
`KUMO_MOMENTUM_ENABLED`. That is a deploy to flip a switch: editing a compose file and restarting a
container to turn a strategy on, with no route from the UI at all. Operator, on seeing it: "do we need to
change code to add a new strategy? omg". Declaring a strategy is necessarily code, because the strategy
CLASS is code. Enabling one must not be.

The legacy `KUMO_MOMENTUM_ENABLED` still gates MOMENTUM in `strategies/momentum.py` and is left alone
here rather than half-migrated; retiring it is tracked on #320.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Tags that are spoken for by live positions and can never be reassigned. Keyed by tag so the
#: collision check reads as "who holds this", which is the question being asked.
_IMMOVABLE: dict[str, str] = {
    "001": "MANUAL — live positions are keyed to MANUAL-001",
    # NOTE 2026-08-16: BCTROT's adapter also DEFAULTS to 003 — the tag I had briefly reserved for it.
    # Cockpit allocated 004 explicitly instead, which is the ownership rule working: two adapters
    # defaulting to the same number is precisely the collision that stops a node booting, and neither
    # repo could see it alone.
    #
    # QC345. Held by the SHIPPED ADAPTER, which hardcodes STRATEGY_TAG = "003"
    # (kumo-trading-strategies runtime/nautilus/qc345_rotation.py), so cockpit allocating anything else would
    # simply disagree with the thing that runs.
    #
    # This tag had an argument. issue 32 is titled "BCTROT-003", so I reserved 003 for a
    # strategy that did not exist and moved QC345 to 004 — then the QC345 adapter landed with 003 baked
    # in. Cockpit owns allocation, and the right exercise of that is to give the tag to the strategy
    # that EXISTS. BCTROT takes the next free tag when it is built; its issue title is a name from
    # before the no-number convention, not a claim.
    "003": "QC345 — the shipped adapter hardcodes this tag",
}


#: Shown beside the UNALLOCATED balance. It is not a strategy — it is capital that has left the
#: strategies without being granted to another, and it must stay visible or the rows stop summing to
#: the account.
UNALLOCATED_TITLE = "Unallocated — freed by a wind-down, not yet granted to any strategy."


class RegistryError(RuntimeError):
    """A declaration that would fail at `Trader.add_strategy`, refused earlier and more clearly."""


#: The cadences a lane may declare (#888). CLOSED: `/strategies` renders the liveness alarm (#349)
#: against this, and an unlisted value would reach the tile as a word nobody defined. `manual` is a lane
#: with no schedule at all; `daily` decides every session; `monthly` decides on the first session of the
#: month and is exit-only otherwise (QC345 — `rebalance_dates()` upstream). Add a value here only with
#: the `_next_rebalance` arm that computes its date, or the row will refuse to build.
CADENCES: frozenset[str] = frozenset({"manual", "daily", "monthly"})

#: The protection stances a lane may declare (#1029). CLOSED, and the SAME set the planner dispatches
#: on — `api.protection.PROTECTION_MODES` is imported here rather than copied, because two copies of
#: one closed set drift, and the drift is invisible until a lane declares the value only one of them
#: knows. `trail` = the account-wide peak-relative stop (what every lane did before #872);
#: `entry_floor` = a fixed stop at the lane's own entry minus the multiple; `none` = the lane owns its
#: own exits and the plane reports its exposure as OPTED OUT — a statement, not an absence.
from api.protection import PROTECTION_MODES as PROTECTION_STANCES  # noqa: E402


@dataclass(frozen=True)
class StrategyEntry:
    """One declared strategy.

    `name` and `tag` are kept separate rather than storing `NAME-tag`, because Nautilus wants them
    separately: `StrategyConfig(strategy_id=name, order_id_tag=tag)`. Storing the joined form would
    mean splitting it again at the one place where getting it wrong stops the node booting.
    """

    name: str
    tag: str
    settings_domain: str
    #: The key this strategy is known by UPSTREAM — how cockpit finds the adapter, and the only thing
    #: it takes from it. Deliberately not the internal id: upstream may rename or renumber itself
    #: without that touching a position, because nothing is keyed on this.
    external_id: str
    #: Human-facing description for the settings UI. Not decorative — an operator toggling a strategy
    #: they cannot identify is the failure this whole registry is meant to prevent.
    title: str
    #: How often this lane DECIDES (#888). One of `CADENCES`. REQUIRED, no default: a default is what
    #: makes a missing declaration invisible, and a lane that omitted it would render as daily on the
    #: tile — on 2026-09-11 that reading took the monthly QC345-003 for a lane dead seven sessions.
    #: Declared here, in cockpit, and checked against the adapter's own rule by
    #: `test_cadence_on_strategies` at test time; `/strategies` carries `cadence_source: "registry"` so
    #: that when the adapter exposes its own (issue 157) a disagreement is representable.
    cadence: str
    #: The lane's PROTECTION STANCE (#1029): what the protection plane rests on this lane's positions
    #: when the tenant's settings file says nothing. One of `PROTECTION_STANCES`. REQUIRED, no default,
    #: for `cadence`'s reason: a default is what made an omitted declaration invisible. SMHGLD-007
    #: declared `PROTECTION_MODE = "none"` in its builder module on 2026-09-11; nothing read that
    #: constant, the settings schema had no key for the lane, and both tenants resolved it to TRAIL —
    #: driven in the paper engine container on 2026-09-12, `mode_of("SMHGLD-007") -> trail`, with the
    #: lane's first live fill two sessions away. This field is what `api.protection.lane_modes` reads
    #: for a lane whose key is absent; the settings key `<lane>_protection` stays the operator override
    #: (#872). The schema's per-lane default must AGREE with this value — pinned by
    #: `test_lane_protection_is_declared_by_the_lane.py`, two derivations, one assertion.
    protection: str
    #: WHY the stance is what it is, for a lane that declares anything but `trail`. Required by
    #: `validate()` for `none` and `entry_floor` — an opt-out with no stated reason is an omission
    #: wearing a declaration's clothes. Empty is legal ONLY for `trail`, the pre-#872 behaviour every
    #: lane inherited; that is a considered default, and this comment is where it is said.
    protection_reason: str = ""

    @property
    def strategy_id(self) -> str:
        return f"{self.name}-{self.tag}"


#: The declared strategies. ORDER IS NOT SIGNIFICANT — anything that depends on it is a bug.
REGISTRY: tuple[StrategyEntry, ...] = (
    StrategyEntry(
        name="MANUAL", tag="001", settings_domain="manual", external_id="MANUAL",
        title="Manual — discretionary clicks. Always live.",
        cadence="manual",
        protection="trail",
    ),
    StrategyEntry(
        # No settings domain: `live_config()` hardcodes MOMENTUM's parameters and reads nothing, so
        # declaring one would assert a configurability that does not exist (#323).
        name="MOMENTUM", tag="002", settings_domain="", external_id="MOMENTUM",
        title="Momentum rotation — daily systematic rotation over a candidate pool.",
        cadence="daily",
        protection="trail",
    ),
    StrategyEntry(
        # BCT rotation — the midday+close schedule, running alongside MOMENTUM-002 as a live
        # comparison. Same pool, same engine, same exits: only identity and schedule differ, so the
        # comparison is ONE change rather than two. the operator's instruction 2026-08-16 is that it replaces
        # MOMENTUM over time with no cutoff, funded by half of MOMENTUM's budget.
        name="BCTROT", tag="004", settings_domain="", external_id="BCTROT",
        title="BCT rotation — midday and close, replacing MOMENTUM over time.",
        cadence="daily",
        protection="trail",
    ),
    StrategyEntry(
        # QC27 Tech Momentum with Inverse Volatility (issue 33, #63). Tag 005 from
        # `next_free_tag`: 001-004 are MANUAL, MOMENTUM, QC345 and BCTROT.
        #
        # This one exercises the ownership rule properly rather than by coincidence. Its adapter has
        # NO default tag at all -- `QC27RotationStrategy.__init__` requires `order_id_tag` and raises
        # without one -- because the activation doc originally proposed TECH_IVOL-003, which is
        # QC345's and would have stopped the node booting. So cockpit allocates and upstream cannot
        # disagree, which is what this registry is for.
        #
        # `external_id` is QC27 (the leaderboard provenance); the internal name is TECHIVOL. Upstream
        # can renumber itself without touching a position, because nothing is keyed on it.
        name="TECHIVOL", tag="005", settings_domain="", external_id="QC27",
        title="Tech IVol — daily tech momentum, inverse-volatility weighted.",
        cadence="daily",
        protection="trail",
    ),
    StrategyEntry(
        # tag 003 happens to match what the adapter currently hardcodes. That is COINCIDENCE, not a
        # dependency — cockpit could allocate 006 and the mapping would carry it. See `external_id`.
        name="QC345", tag="003", settings_domain="qc345", external_id="QC345",
        title="QC345 — monthly top-down momentum rotation.",
        # MONTHLY — `rebalance_dates()` upstream: first session of the month, exit-only otherwise.
        cadence="monthly",
        protection="trail",
    ),
    StrategyEntry(
        # CRSISHORT (issue 123, platform issue 858). Tag 006 from `next_free_tag`. THE FIRST
        # LANE THAT HOLDS THE SHORT SIDE — `ownership.SHORT_PERMITTED` names it and only it.
        # Registered in SHADOW: its order path (a resting short LIMIT, issue 131) does
        # not exist yet, and the gateway refuses TRADING until it does.
        name="CRSISHORT", tag="006", settings_domain="", external_id="CRSISHORT",
        title="CRSI short — ConnorsRSI>90 / >100% vol, 20-slot short book. SHADOW until #131.",
        cadence="daily",
        # NONE (#1029, lead's ruling 2026-09-12): the adapter owns this lane's exits — a per-short
        # trail carried since entry and the flat cover (`crsi_short.py` `_trail` /
        # `evaluate_short_exits`), exactly SMHGLD's reason. Whether the plane can HONOUR the stance
        # on a short row is #1035 (the plane is long-only today); the declaration is true either way.
        protection="none",
        protection_reason=(
            "CRSISHORT carries its own per-short trail and flat cover in the adapter "
            "(kumo-trading-strategies crsi_short.py); a broker stop on a short is a BUY the protection plane "
            "cannot attribute to a lane, and a second exit rule beside the adapter's would race it."),
    ),
    StrategyEntry(
        # SMHGLD (issue 177, platform issue 953/#965). Tag 007 from `next_free_tag`. The
        # fixed-weight SMH/GLD sleeve: decides at close-20m, trades by target and delta.
        name="SMHGLD", tag="007", settings_domain="", external_id="SMHGLD",
        title="SMHGLD — a fixed-weight SMH/GLD sleeve (risk_weight from the upstream config), rebalanced on drift.",
        cadence="daily",
        # NONE, BY DECLARATION (coordinator's ruling, #953; wired here by #1029). This used to live as
        # `PROTECTION_MODE = "none"` in `strategies/smhgld.py`, read by nothing.
        protection="none",
        protection_reason=(
            "SMHGLD is a fixed-weight two-ETF sleeve that rebalances ~93 times a year and never exits; "
            "its measured design carries no stop (the drawdown figures already include having none), "
            "and a stop sized to the whole holding would be tripped by the lane's own trims (the "
            "2026-08-12 PEAK cancel-and-replace class). 'none' is a declared stance so the protection "
            "plane reports the exposure as opted_out — a statement, not an absence. With no stops the "
            "#873 hooks and the operator flatten are the whole safety surface, and a flatten is UNDONE "
            "by the next decision: to stop this lane, halt or deregister it."),
    ),
)


def validate(entries: tuple[StrategyEntry, ...] = REGISTRY) -> None:
    """Refuse anything Nautilus would refuse, here rather than at node startup.

    `add_strategy` raises on a duplicate tag, which means the failure surfaces as a node that will not
    boot — during a deploy, with the market open, from a mistake made in a file that looked fine. The
    same check costs nothing at import time and names the conflict.
    """
    seen_tags: dict[str, str] = {}
    seen_names: set[str] = set()
    for e in entries:
        if not e.tag.isdigit() or len(e.tag) != 3:
            raise RegistryError(
                f"{e.name}: order_id_tag must be three digits (got {e.tag!r}) — it is the suffix of "
                "every client order id and of the StrategyId"
            )
        if e.tag in seen_tags:
            raise RegistryError(
                f"order_id_tag {e.tag!r} claimed by both {seen_tags[e.tag]} and {e.name}. Nautilus "
                "rejects this at Trader.add_strategy and the node will not start."
            )
        held_by = _IMMOVABLE.get(e.tag)
        if held_by is not None and not held_by.startswith(e.name):
            raise RegistryError(
                f"{e.name} claims tag {e.tag!r}, which belongs to {held_by}. Reassigning it re-homes "
                "existing positions, because the NETTING position id is {instrument}-{strategy_id}."
            )
        if e.name in seen_names:
            raise RegistryError(f"strategy name {e.name!r} declared twice")
        if e.cadence not in CADENCES:
            raise RegistryError(
                f"{e.name}: cadence {e.cadence!r} is not one of {sorted(CADENCES)} — the tile and the "
                "liveness alarm read this word, and an undefined one renders as daily (#888)"
            )
        if e.protection not in PROTECTION_STANCES:
            raise RegistryError(
                f"{e.name}: protection {e.protection!r} is not one of {sorted(PROTECTION_STANCES)} — "
                "the protection plane cannot dispatch on it and would rest the default (#1029)"
            )
        if e.protection != "trail" and not e.protection_reason.strip():
            raise RegistryError(
                f"{e.name}: protection {e.protection!r} declared with no protection_reason — an opt-out "
                "with no stated reason is an omission wearing a declaration's clothes (#1029)"
            )
        seen_tags[e.tag] = e.name
        seen_names.add(e.name)


def next_free_tag(entries: tuple[StrategyEntry, ...] = REGISTRY) -> str:
    """The lowest three-digit tag no declared strategy holds.

    Allocation belongs here rather than in a comment telling the next author to count. Nothing enforces
    contiguity — a removed strategy leaves its tag reusable only if nothing it traded still exists,
    which is a judgement this function deliberately does not make. It returns the lowest FREE tag; the
    `_IMMOVABLE` check in `validate` is what stops a genuinely spoken-for one being taken.
    """
    taken = {e.tag for e in entries} | set(_IMMOVABLE)
    for n in range(1, 1000):
        tag = f"{n:03d}"
        if tag not in taken:
            return tag
    raise RegistryError("no free order_id_tag below 999")


def assign_tag(name: str, entries: tuple[StrategyEntry, ...] = REGISTRY) -> str:
    """The tag for a strategy NAME — the allocation entry point upstream should ask for.

    Idempotent: a name already registered keeps its tag, because the id is permanent once anything has
    traded under it (the NETTING position id is `{instrument}-{strategy_id}`, so a changed tag re-homes
    every position it owns). A new name gets the lowest tag that is neither taken nor reserved.

    Deliberately NOT derived from the name — a hash would be stable and collision-prone and unreadable,
    and the tag has to be typed into `StrategyConfig` and read off a client order id by a human.
    """
    for e in entries:
        if e.name == name:
            return e.tag
    for tag, held_by in _IMMOVABLE.items():
        if held_by.startswith(name):
            return tag
    return next_free_tag(entries)


def by_external_id(external_id: str, entries: tuple[StrategyEntry, ...] = REGISTRY) -> StrategyEntry | None:
    """Find a registered strategy by the key UPSTREAM knows it as.

    This is the ONLY thing cockpit should take from an adapter. Reading its internal tag and matching
    on that makes the adapter the authority on an id cockpit owns, and turns an upstream rename into a
    cockpit migration.
    """
    for e in entries:
        if e.external_id == external_id:
            return e
    return None


def by_id(strategy_id: str, entries: tuple[StrategyEntry, ...] | None = None) -> StrategyEntry | None:
    # `entries=None`, resolved INSIDE: a default of `REGISTRY` binds the tuple at definition time, so a
    # test that re-declares the registry (`monkeypatch.setattr(strategy_registry, "REGISTRY", ...)`)
    # would be read by nobody and every caller would keep answering from the import-time copy — the
    # default-argument shape CLAUDE.md names (#1029, found by the hardcoded-lane mutant).
    for e in (REGISTRY if entries is None else entries):
        if e.strategy_id == strategy_id:
            return e
    return None


def claim_conflicts(claims: dict[str, set[str]]) -> list[str]:
    """Instruments claimed by more than one strategy, as `external_order_claims` would see them.

    Reported rather than raised, because the caller decides. Claims are what let a strategy adopt the
    synthetic flatting order reconciliation generates for its own position — without one, that order is
    booked under `EXTERNAL` and, under NETTING, OPENS a phantom position instead of closing the real
    one. So claiming too little is also a defect; this only reports the case Nautilus will reject.
    """
    seen: dict[str, str] = {}
    conflicts: list[str] = []
    for strategy_id in sorted(claims):
        for instrument in sorted(claims[strategy_id]):
            if instrument in seen:
                conflicts.append(
                    f"{instrument} claimed by both {seen[instrument]} and {strategy_id}"
                )
            else:
                seen[instrument] = strategy_id
    return conflicts


# SELF-CHECK AT IMPORT (#888 review). Every refusal above was reachable from tests only — `validate()`
# had no production caller, so the docstring's "here rather than at node startup" was not true. A
# registry that would not boot now fails the import of this module, in the api and the engine alike.
validate()
