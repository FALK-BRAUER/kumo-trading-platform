"""Who may cancel whose resting order (#462, same root as #437).

`_cancel_reducing_leg` cancels natively for orders the cache still holds open, then sweeps the VENUE for
the rest — orders the cache has written off that still reserve shares. The native half is scoped by
strategy. The venue half is not, so an exit on a shared symbol cancels whatever else is resting there.

WHY "CANCEL ONLY YOUR OWN" IS WRONG. Measured on the live durable cache 2026-08-22, nine of ten
protective stops belong to MANUAL-001 and one to MOMENTUM-002. Account-level protection is placed by the
reconciler running under MANUAL — #437's decision, taken because NETTING position ids are per-strategy
and no single correct owner exists for a stop covering an account net. So the order reserving an exiting
lane's shares is almost never that lane's own, and refusing everything foreign would refuse every
legitimate exit.
"""

from __future__ import annotations

#: The strategy the account-level protection reconciler runs under. NAMED TO THE CONSTRUCTION, not to a
#: module — #437's wording — because the exemption is for one specific mechanism, and a module-level
#: carve-out would exempt everything that happens to live beside it.
PROTECTION_OWNER = "MANUAL-001"


def may_cancel(*, owner: str | None, canceller: str) -> bool:
    """May `canceller` cancel a resting order belonging to `owner`?

    UNATTRIBUTABLE IS REFUSED. Cockpit's ids are `PROT-SELL-{SYMBOL}-{hash}` and kumo-strategies' are
    `kumo-{sha1}` with the owner hashed IN and not recoverable OUT — so when the cache cannot name an
    order, neither repo's id can either. Refusing is safe by `release_for_exit`'s own standard: the exit
    not going out is recoverable, a position left unprotected is not.
    """
    # NO EXPLICIT `if not owner` GUARD, and that is deliberate. It read as defence and could not change
    # the outcome: an unattributable owner falls through both comparisons to False anyway, so a mutation
    # deleting it left every test green. A check that cannot fail is indistinguishable from one that does
    # not work — the behaviour is asserted by
    # `test_an_UNATTRIBUTABLE_order_is_REFUSED` instead, which is what actually matters.
    if owner == canceller:
        return True
    # THE ONE EXEMPTION. Account-level protection is what holds this lane's shares, and it is
    # account-scoped deliberately.
    return owner == PROTECTION_OWNER


def may_cancel_order(*, owner: str | None, canceller: str, row: dict,
                     canceller_is_proxy: bool = False) -> bool:
    """May `canceller` cancel this specific resting order?

    OWNER ALONE IS TOO WIDE, found in review. `may_cancel` on the owner lets any lane cancel ANY
    MANUAL-001 order — including a discretionary sell placed by hand, which is not protection and which
    nothing re-arms. The operator trades this account by hand under MANUAL-001, so that is a live shape rather
    than a hypothetical.

    THE PREFIX IS NOT THE ANSWER EITHER, and the evidence against it is stronger than the evidence for
    it. #242: a bracket leg carries a client id ALPACA minted, not ours, so `PROT-SELL` holds for orders
    WE construct and provably not for orders the venue constructs on our behalf. Keying on it would
    call those unattributable and refuse — making every bracketed position unexitable.

    So the exemption requires BOTH: the owner is the protection construction AND the order is protective
    by TYPE. A discretionary exit is a market or limit order, never a stop. A lane's OWN orders are not
    type-restricted — cancelling its own resting limit is its business.
    """
    # Same as `may_cancel`: no explicit unattributable guard, because it cannot change the outcome.
    if owner == canceller:
        return True
    from api.protection import PROTECTIVE_TYPES

    # THE PROXY CONCESSION, and it is a NAMED GAP rather than a design.
    #
    # `release_for_exit` cannot know which lane is asking: `NautilusBroker.exit` calls
    # `feed.release_for_exit(str(iid), int(req.qty))` with no strategy_id, deliberately, and the feed
    # then uses `owner = str(self.id)` — MANUAL-001 — for every exit from every lane. So "MOMENTUM
    # exiting its own stop" and "MOMENTUM exiting BCTROT's" arrive IDENTICAL.
    #
    # Without this, the rule refused the only stop resting on the live book: PROT-SELL-BDX-XNYS-3311fe07,
    # owned by MOMENTUM-002, 55 shares — a lane's own protection, unreachable through its own exit.
    #
    # The concession is wide and the TYPE check is the only thing holding it, so a discretionary sell is
    # still refused. #462 remains unfixed on THIS ONE PATH; the manager and PEAK paths pass the real lane
    # and get the strict rule. The durable fix is threading `req.strategy_id` through, which is
    # cross-repo — and `test_the_PROXY_concession_is_the_NAMED_GAP_in_462...` fails the day it lands.
    if canceller_is_proxy and canceller == PROTECTION_OWNER:
        kind = str((row or {}).get("order_type") or (row or {}).get("type") or "")
        return kind in PROTECTIVE_TYPES
    if owner != PROTECTION_OWNER:
        return False

    # BOTH KEYS. Alpaca returns `type`; our own frames emit `order_type`. Reading one calls half the
    # rows untyped — the same bilingual problem `is_resting` already solves for status.
    kind = str((row or {}).get("order_type") or (row or {}).get("type") or "")
    return kind in PROTECTIVE_TYPES


#: Client-order-id prefixes this system places protective stops under. MIRRORS `engine_node._OURS`, and
#: a test asserts the two sets are identical — a private copy would drift the day a fifth is added, and
#: the list has already gone from two to four.
OUR_STOP_PREFIXES = ("PROT-", "PKW-", "PK-", "FL-")


def owner_from_prefix(coid: str) -> str | None:
    """Attribution of LAST RESORT, for orders the cache can no longer answer for.

    THE VENUE SWEEP EXISTS FOR EXACTLY THOSE ORDERS, so a cache miss is not an edge case there — it is
    the central case. Refusing on a miss turns the sweep off precisely where it was needed, which is how
    PEAK lost the ability to re-arm over its own stuck stop: `PKW-ours-but-stuck` is stuck BECAUSE the
    cache cannot answer.

    Every prefix here is minted by `UiFeedStrategy`, which IS MANUAL-001 — the feed places protection,
    which is also why nine of ten live stops carry that id. So a prefix match means account-level
    protection.

    #242 CUTS AGAINST THE PREFIX AS A SOLE KEY — a bracket leg carries an id ALPACA minted, so ours is
    absent — and this is not a sole key: it is the fallback when the primary source is blind, the type
    check still applies on top, and an id matching nothing stays unattributable and refused. An order we
    did not place is not ours to cancel on either path.
    """
    c = str(coid or "")
    return PROTECTION_OWNER if c.startswith(OUR_STOP_PREFIXES) else None
