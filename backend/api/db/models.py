"""App-data ORM models. First table: the runtime watchlist (the reason Postgres exists now) — user-edited
symbols, distinct from the immutable layout config (config-over-editor stays true for LAYOUT; the watchlist
is user data, not layout).

Second: the trade-cycle ENVELOPE (#74) — engine-owned cycle METADATA. This is a THIRD category, distinct from
both: it is NOT native trade state (Nautilus owns positions/orders/fills) and NOT a P&L ledger (P&L stays
derived from native). It stores only the cycle boundary + lifecycle that Nautilus can't express (which flat was
bridged = same cycle), so the engine rebuilds identical TradeDTO state after restart."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base


class WatchlistItem(Base):
    """One symbol on the runtime watchlist. `instrument_id` is the canonical TICKER.MIC (e.g. AAPL.XNAS)."""

    __tablename__ = "watchlist_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TradeCycleEnvelope(Base):
    """The durable cycle envelope (#74) — engine-owned domain metadata, written by the ENGINE process on cycle
    lifecycle transitions and read on startup to seed the TradeCycleProjection so restart reproduces the SAME
    cycle_id + boundary. NOT native trade state, NOT a P&L ledger (no qty/price/realized — P&L is recomputed
    from native on restart via the persisted position snapshots).

    `cycle_id` is the opaque projection id (its own PK). `opened_ts` is the cycle-open anchor (the boundary
    native state can't recover). `last_event_ts` guards idempotent at-least-once writes (a stale write is
    ignored). `import_status` flags cycles that couldn't be cleanly attributed on restart (needs_review) rather
    than silently binding them to an old cycle."""

    __tablename__ = "trade_cycle"

    # {account}:{client}:{instrument}:{strategy}:{ts} — sized above the sum of the component column widths.
    cycle_id: Mapped[str] = mapped_column(String(280), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), index=True)
    client_id: Mapped[str] = mapped_column(String(64))
    instrument_id: Mapped[str] = mapped_column(String(32), index=True)
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    opened_ts: Mapped[int] = mapped_column(BigInteger)  # cycle-open anchor (ns) — the cycle_id anchor
    closed_ts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # None while the cycle is live
    state: Mapped[str] = mapped_column(String(8))  # last known: HELD | ARMED | WATCH | CLOSED
    close_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)  # manual_walk_away | …
    close_reason_source: Mapped[str | None] = mapped_column(String(12), nullable=True)  # default | user | strategy
    import_status: Mapped[str] = mapped_column(String(16), default="ok")  # ok | needs_review
    last_event_ts: Mapped[int] = mapped_column(BigInteger)  # monotonic guard for idempotent writes
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CommandLedgerEntry(Base):
    """Durable command idempotency ledger (#78). In-memory dedup (`_seen_orders`) is lost on restart, so a
    restart + still-pending ui:commands entries could DOUBLE-SEND an order. The engine RESERVES a row here
    before acting on a command; a re-delivered `command_id` (transport dup) or a re-used `client_order_id`
    (economic dup) is skipped. Engine-owned metadata — NOT native order state (Nautilus owns that)."""

    __tablename__ = "command_ledger"

    command_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # transport idempotency key (#32/#39)
    command_type: Mapped[str] = mapped_column(String(24))  # submit_order | cancel_order | modify_order | …
    # Economic idempotency key — one order per client_order_id, even across different command_ids. Unique when
    # present (order commands); null for non-order commands (e.g. stream_request).
    client_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    payload_hash: Mapped[str] = mapped_column(String(64))  # canonical payload hash — detect same-id diff-payload
    entry_id: Mapped[str] = mapped_column(String(64))  # the ui:commands Redis stream entry id
    status: Mapped[str] = mapped_column(String(12))  # RESERVED | DONE | REJECTED
    error: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PositionTransferEvent(Base):
    """Append-only log of internal position transfers between strategies (#80 spin-off).

    A transfer MOVES quantity from one strategy to another with paired internal fills. No order reaches the
    venue and the broker net is unchanged; what changes is which strategy natively owns the position, so the
    target can then manage it like anything else it holds.

    Outbox semantics, not just audit. `ExecutionEngine.process()` is queued and not transactional, so a crash
    between the two legs would leave the books split. The sequence PREPARED → SOURCE_APPLIED → DEST_APPLIED →
    COMPLETED (or FAILED) lets startup recovery see exactly how far a transfer got and finish it — using the
    deterministic leg order ids to check what the cache already has.

    Supersedes `position_claim_event`, whose rows are kept as audit of the earlier attribution-only approach.
    Those rows never moved a position and must not be reinterpreted as transfers.
    """

    __tablename__ = "position_transfer_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(16))  # PREPARED|SOURCE_APPLIED|DEST_APPLIED|COMPLETED|FAILED
    transfer_id: Mapped[str] = mapped_column(String(64), index=True)
    command_id: Mapped[str] = mapped_column(String(64), index=True)  # transport idempotency

    account_id: Mapped[str] = mapped_column(String(64))
    client_id: Mapped[str] = mapped_column(String(64))
    instrument_id: Mapped[str] = mapped_column(String(32), index=True)
    source_strategy_id: Mapped[str] = mapped_column(String(64))
    target_strategy_id: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(8))  # LONG | SHORT — the side being moved
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 10))

    pricing_mode: Mapped[str] = mapped_column(String(12))  # MARKET | CARRY_OVER
    # Programmatically sourced, never operator-supplied: whoever picks the price picks which strategy keeps
    # the P&L, so letting a human type it would make transfers a P&L-shifting tool.
    transfer_px: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    source_avg_px: Mapped[Decimal] = mapped_column(Numeric(28, 10))  # basis at transfer time, for audit

    # Deterministic per transfer — recovery looks these up in the Nautilus cache to see which legs landed.
    source_order_id: Mapped[str] = mapped_column(String(96))
    dest_order_id: Mapped[str] = mapped_column(String(96))
    source_ts_last: Mapped[int] = mapped_column(BigInteger)  # stale-read guard

    reversal_of_transfer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64))  # server-stamped, never a request field
    error: Mapped[str | None] = mapped_column(String(256), nullable=True)
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Manager(Base):
    """A manager instance — the generic automation primitive #55 specified (attach->watch->trigger->act with
    a leash), replacing the flatten-specific `queued_flatten` this superseded. Current state only; the full
    history lives in `ManagerEvent`. See `api.managers` for the lifecycle this backs.

    `cycle_id` is load-bearing, not decorative: an `apply()` whose live cycle no longer matches this is
    refused rather than executed — a coincidental same instrument/strategy/side/qty match on a NEW cycle
    (the position closed and reopened while this manager sat ARMED) is not the intent that was confirmed."""

    __tablename__ = "manager"

    manager_id: Mapped[str] = mapped_column(String(36), primary_key=True)  # own UUID, never a command_id
    kind: Mapped[str] = mapped_column(String(32), index=True)
    kind_version: Mapped[int] = mapped_column(Integer, default=1)
    account_id: Mapped[str] = mapped_column(String(64))
    client_id: Mapped[str] = mapped_column(String(64))
    instrument_id: Mapped[str] = mapped_column(String(32), index=True)
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    cycle_id: Mapped[str | None] = mapped_column(String(280), nullable=True)
    leash: Mapped[str] = mapped_column(String(8))  # AUTO | CONFIRM | ALERT
    params: Mapped[dict] = mapped_column(JSONB)  # kind-specific config
    # ARMED | PROPOSED | APPROVED | APPLYING | APPLIED | FAILED | CANCELLED
    state: Mapped[str] = mapped_column(String(12), index=True)
    last_event_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ManagerEvent(Base):
    """Append-only history for one `Manager` — the outbox half of the pair. An INTENT_RECORDED event lands
    BEFORE any native effect (order submit), carrying the deterministic order id, so a crash between
    submitting and recording the outcome is recoverable by checking the durable Nautilus cache for that id
    rather than guessed at — the same lesson `PositionTransferEvent` already taught this codebase."""

    __tablename__ = "manager_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    manager_id: Mapped[str] = mapped_column(String(36), index=True)
    # ATTACHED | PROPOSED | APPROVED | REJECTED | INTENT_RECORDED | APPLIED | FAILED | CANCELLED
    event_type: Mapped[str] = mapped_column(String(16), index=True)
    # For readability/audit joins on human-initiated events — the CommandLedgerStore (#78) is the actual
    # idempotency guard, not this column.
    command_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExecutionQualityLeg(Base):
    """One symbol-side on one session, priced against that morning's official opening auction (#210).

    Lives in Postgres rather than a CSV on a volume, deliberately. The point of this table is a sample
    that ACCUMULATES over 30-60 sessions and is then analysed — grouped by session, by side, by lag —
    which is SQL's job. A CSV would also live inside one container's filesystem, where the next rebuild
    silently resets the measurement to zero and nothing would say so.

    Written after the close by a read-only job. Nothing in the trading path touches this table, and
    losing it entirely would cost a research sample and no money.
    """

    __tablename__ = "execution_quality_leg"
    # Mirrors migration 0012. Without it alembic autogenerate proposes dropping the constraint on the
    # next unrelated migration.
    __table_args__ = (
        UniqueConstraint("session", "symbol", "side", name="uq_execution_quality_leg"),
        {"comment": "drift between our fills and the official opening auction (#210)"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session: Mapped[str] = mapped_column(String(10), index=True)
    symbol: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    fill_vwap: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    auction_px: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    auction_exchange: Mapped[str] = mapped_column(String(8))
    auction_size: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    first_fill_utc: Mapped[str] = mapped_column(String(40))
    #: Minutes between the official opening print and our first fill — the quantity the whole study is
    #: about. The backtest assumes 0; live has been running 5-6.
    lag_minutes: Mapped[Decimal] = mapped_column(Numeric(10, 3))
    #: Signed so positive always means WORSE than the auction, whichever side we traded.
    drift_bps: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StrategySleeve(Base):
    """One strategy's capital allocation — the durable form of `api.budget.Sleeve` (#320).

    Two numbers, and the gap between them is the instruction. `target` is operator intent: it changes the
    instant someone decides and moves no capital. `actual` is the sleeve's net asset value and moves only when
    a fill actually frees or commits money. So a transition cannot double the book's exposure — the recipient
    can only deploy what the donor has genuinely handed over.

    NOT the same thing as `position_transfer_event`, which moves POSITION OWNERSHIP between strategies (#80).
    That changes who natively holds a position; this changes how much capital each may deploy. Both are
    "transfers between strategies" and conflating them would be easy, so: ownership there, allocation here.
    """

    __tablename__ = "strategy_sleeve"

    strategy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    actual: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SleeveTransfer(Base):
    """Append-only log of capital moving between sleeves, and the idempotency guard for it (#320).

    `fill_id` is UNIQUE, and that constraint IS the guard. Fills are not delivered exactly once — a
    reconciliation pass re-reports them and a reconnect replays them — and applying one twice pushes the donor
    below its target and the recipient above its headroom while each application looks individually correct.
    An in-memory set would forget across a restart, which is exactly when reconciliation replays hardest, so
    the uniqueness lives in the database where a duplicate insert simply fails.
    """

    __tablename__ = "sleeve_transfer"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: The venue fill this transfer was computed from. UNIQUE — see the class docstring.
    fill_id: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    from_strategy: Mapped[str] = mapped_column(String(64), index=True)
    #: May be `UNALLOCATED` — capital that has left the strategies but not the account.
    to_strategy: Mapped[str] = mapped_column(String(64), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    reason: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


#: The capture kinds that are window BASES — the rows a period return subtracts from. ONE
#: declaration: the predicate below is DERIVED from this tuple rather than written beside it.
BASE_CAPTURE_KINDS = ("close", "reconstructed-eod")

#: The same rule as LITERAL SQL, for the partial index AND for the `on_conflict_do_nothing` target.
#:
#: IT MUST NOT CARRY BIND PARAMETERS. `capture_kind.in_(BASE_CAPTURE_KINDS)` compiles to
#: `WHERE capture_kind IN ($20, $21)`, and Postgres has to PROVE the ON CONFLICT predicate implies
#: this index's predicate — which it can only do while it knows the values. It plans a prepared
#: statement with the values in hand for the first five executions and then considers a GENERIC plan
#: without them, so the SIXTH row raises `InvalidColumnReferenceError` and the first five do not.
#: Paper holds fourteen positions; the capture would have died every evening at row six while the
#: one-row integration test passed forever.
BASE_KIND_PREDICATE = "capture_kind IN ({})".format(", ".join(f"'{k}'" for k in BASE_CAPTURE_KINDS))


class EodPositionObservation(Base):
    """WHAT THE BOOK LOOKED LIKE AT A CAPTURE — an OBSERVATION LOG, not a ledger (#734).

    Exists so a lane's window P&L can be `realized(W) + Δunrealized(W)`, the same identity the account
    hero uses. `Δunrealized(W, lane)` needs the lane's holdings and their basis at the window's START,
    and the broker publishes ACCOUNT-level curves only — so it is observed here, per position, per day.

    NOT A PARALLEL LEDGER, and the distinction is conditional rather than a matter of intent. It holds
    only while this table is: append-only, dated, never read by trading logic, and never served as
    current positions. `Cache`+`Position` remain the source of truth for what is held; this is the
    same species as a broker's `portfolio_history`. The moment a governing mechanism reads it — a heat
    cap, a risk gate — that stops being true and this becomes the thing CLAUDE.md forbids.

    COMPONENTS, NEVER THE CONCLUSION. Δunrealized is derived at read time and is not stored. A stored
    derived number cannot be audited, cannot be re-derived when a rule changes, and has nothing to
    disagree with — which is exactly the condition under which a severed wire is invisible. For the
    same reason `basis_contested` (#370) is NOT a column: both raw bases are stored and the flag is
    derived, because a stored flag is a frozen conclusion.

    THE ENGINE COLUMNS ALONE WOULD MAKE THIS SELF-CONGRATULATORY. Rows written from the engine's own
    cache observe the ENGINE, not reality — an engine defect would be faithfully logged as truth. The
    `*_venue` columns are what give every row its own engine-vs-broker diff, and they are the reason
    this is an observation rather than a second opinion of the first.
    """

    __tablename__ = "eod_position_observation"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    #: The ET TRADING date, computed engine-side. The operator's clock is SGT, so a local date would
    #: put a 04:00 SGT close on the wrong calendar day.
    session_date: Mapped[str] = mapped_column(String(10), index=True)
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    instrument_id: Mapped[str] = mapped_column(String(32), index=True)

    #: WHICH JOB WROTE THIS ROW — provenance, not a verdict. `close` | `intraday-<slot>` |
    #: `reconstructed-eod`. It is what lets the window BASE be identified without a read-time rule
    #: that can drift or a stored conclusion that cannot be re-derived; same pattern as
    #: `exec_action_log.slot`, which this codebase already relies on.
    capture_kind: Mapped[str] = mapped_column(String(32))
    #: Capture drift must be VISIBLE, not folded into `session_date`.
    snapshot_ts: Mapped[int] = mapped_column(BigInteger)

    qty: Mapped[float] = mapped_column(Float)                      # signed — shorts negative
    #: BOTH raw bases, three-state. NULL means "not asked", which is not "agrees" (#370).
    avg_px_engine: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_px_venue: Mapped[float | None] = mapped_column(Float, nullable=True)
    qty_venue: Mapped[float | None] = mapped_column(Float, nullable=True)  # the engine-vs-reality wire

    mark_px: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: `live` | `close` | `reconstructed-bar`. A close-to-close series must never silently contain a
    #: live quote; the read path filters on `capture_kind`, and this is how an audit sees which it got.
    mark_source: Mapped[str | None] = mapped_column(String(24), nullable=True)
    mark_ts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str] = mapped_column(String(8))   # staging is SGD — no default; say it

    #: THREE DERIVATIONS OF ONE FACT, stored as observations rather than as truth. Components-derived
    #: `qty x (mark − basis)` is computed at read time; these two are what it is checked against. Two
    #: of them disagreeing is the detector — #370's WHD divergence (+263.84 against the broker's
    #: +9.52) would have been visible daily instead of found forensically.
    unrealized_engine: Mapped[float | None] = mapped_column(Float, nullable=True)
    unrealized_venue: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: Net qty this lane traded in this instrument this session. Without it a qty jump between two
    #: adjacent days is AMBIGUOUS with an ordinary fill; with it,
    #: `qty_t == qty_{t-1} + session_fill_qty_t + transfers_t` is checkable and a violation IS the
    #: corporate-action detector. "Splits show up" is otherwise a human squinting at a table.
    session_fill_qty: Mapped[float] = mapped_column(Float)

    cycle_id: Mapped[str | None] = mapped_column(String(280), nullable=True)  # joins trade_cycle
    quality: Mapped[str | None] = mapped_column(String(16), nullable=True)    # adopted | native
    provenance: Mapped[str] = mapped_column(String(16))                       # engine | reconstruction
    #: Stamps the rule this row was derived under. A re-run backfill with a corrected rule writes rows
    #: under a NEW version and never updates in place — updating would break both append-only and the
    #: re-derivability this table exists for.
    method_version: Mapped[str] = mapped_column(String(24))
    #: Fraction of this row's qty whose opener is known. 1.0 for engine rows. Reconstructed rows
    #: before the 100%-attribution boundary carry less, permanently and visibly.
    attribution_coverage: Mapped[float] = mapped_column(Float)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        #: ONE base row per lane-instrument-day PER DERIVATION. Intraday captures are observations and
        #: are deliberately NOT eligible: a missing close row must be a REFUSED base — an em dash until
        #: backfill supplies one — never "the nearest intraday row".
        #:
        #: `method_version` IS IN THE KEY, and leaving it out was a defect caught in review. The
        #: comment on `method_version` promises a corrected re-derivation "writes rows under a NEW
        #: version and never updates in place" — and without it in the key, that rerun is REFUSED by
        #: this very index. Two guarantees in one commit contradicting each other, with the database
        #: enforcing the wrong one. Proven against a real Postgres before it was fixed.
        Index(
            "uq_eod_observation_base",
            "session_date", "strategy_id", "instrument_id", "method_version",
            unique=True,
            postgresql_where=text(BASE_KIND_PREDICATE),
        ),
    )


class EodObservationManifest(Base):
    """DID THE CAPTURE RUN, and what did it see (#734).

    A lane holding nothing must be an explicit "observed, 0 positions". Without this table a flat lane
    and a capture that never ran are the same absence — and absence readable as flatness is this
    repo's most-repeated defect class. Three states, never two: `observed` | `failed` | `absent`.
    """

    __tablename__ = "eod_observation_manifest"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_date: Mapped[str] = mapped_column(String(10), index=True)
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    capture_kind: Mapped[str] = mapped_column(String(32))
    snapshot_ts: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))          # observed | failed | absent
    instrument_count: Mapped[int] = mapped_column(Integer)
    #: Why a capture failed, in the operator's words. NULL on `observed`.
    detail: Mapped[str | None] = mapped_column(String(256), nullable=True)
    provenance: Mapped[str] = mapped_column(String(16))
    method_version: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    #: NOT UNIQUE, and a unique index here was a defect. Forcing one outcome per (date, lane) meant a
    #: close capture that FAILED at 16:20 could never be followed by a successful retry at 16:30 — the
    #: repair was refused and the day stayed frozen as `failed` while the observation rows beside it
    #: were correct. Two sources disagreeing, manufactured by the table meant to prevent that.
    #:
    #: A failed attempt and a later successful one are BOTH true. This is an ATTEMPT log: the day's
    #: status is the latest attempt, and the earlier failure stays visible, which is what makes a
    #: transient outage auditable instead of erased.
    __table_args__ = (
        Index("ix_eod_manifest_lookup", "session_date", "strategy_id", "capture_kind"),
    )
