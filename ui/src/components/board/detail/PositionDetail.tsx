"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useSource } from "@/lib/framework/datasource/useSource";
import { CANCEL_THEN_ACT_TIMEOUT_MS, useCommandStatus } from "@/lib/framework/useCommandStatus";
import { toggleError, togglePending } from "@/lib/framework/toggleStatus";
import {
  flattenPosition,
  getManagers,
  searchInstruments,
  attachManager,
  cancelManager,
  type FlattenPayload,
  type Manager,
} from "@/lib/api/client";
import { useInstrument, computePnl } from "@/lib/framework/instrument";
import { magnitude, sideSign, signedQty } from "@/lib/framework/signedQty";
import { useCockpitStore } from "@/lib/framework/store";
import { pnlToneClass } from "@/components/ds/pnl";
import { SlideToConfirm } from "@/components/ds/SlideToConfirm";
import { positionKey, strategyLabel } from "@/lib/framework/position";
import { lastLevels } from "@/lib/ichimoku";
import { stopReenterDefaults } from "@/lib/stopReenter";
import { usePeakParams } from "@/lib/peakDefaults";
import { pyramidDefaults } from "@/lib/pyramidDefaults";
import type { DetailProps } from "@/lib/framework/detail/registry";
import type { OrderDTO, PositionDTO, PositionsResponse, TradeDTO } from "@/lib/api/types";
import { DetailIdentity, venueOf } from "./DetailIdentity";
import { CloseButton } from "@/components/ds/CloseButton";

/**
 * Position detail (#70/#71) — the POSITION-kind detail variant. Split into a pure presentational View
 * (mockable in /dev/ui — there are usually 0 open paper positions to browser-verify against) and the
 * descriptor, which re-reads the position LIVE by positionKey `${strategy_id}:${instrument_id}` from the
 * `positions` source + the live mark price, so P&L stays current while open.
 *
 * Flatten (#170 first slice) lives here — a slide, not a tap, sized against the LIVE position at the moment
 * of submit. Outside regular hours it ATTACHES a `deferred_flatten` MANAGER (#55) instead of placing an
 * order immediately; `queuedFlatten` reads that manager's state from `GET /managers` (the ack for the
 * original enqueue expires long before an overnight manager fires, so a re-opened screen can't rely on it).
 * Trim/adjust-stop are still #170's later slices.
 * Cycle state / protective stops are #73/#77 (not on the PositionDTO yet) — shown as pending, not faked.
 */

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 py-1.5">
      <span className="font-mono text-[10px] uppercase tracking-wide text-t3">{label}</span>
      <span className="font-mono text-[12px] text-t1">{children}</span>
    </div>
  );
}

/**
 * Why a manager toggle did not take (#256). Renders nothing when there is nothing to say, so the toggles
 * keep their current height in the ordinary case.
 *
 * This is the whole fix for the complaint that PYRAMID "seems not working": it was refusing every arm and
 * saying so to nobody, which is indistinguishable from a dead button. Sits inside the toggle's own box so
 * the reason is unambiguously attached to the control that produced it — three toggles stack here, and a
 * floating error line would be ambiguous between them.
 */
function ToggleError({ message }: { message?: string | null }) {
  if (!message) return null;
  return (
    <div role="alert" className="mt-1.5 font-mono text-[10px] leading-snug text-status-bear">
      {message}
    </div>
  );
}

/**
 * Pick PYRAMID's driver symbol by typing a ticker (#38).
 *
 * This was a bare text input that required a full Nautilus `InstrumentId` — `SMH.XNAS`, venue and all —
 * and answered anything else with the raw constructor error:
 *
 *     invalid `InstrumentId` value 'SMH': missing '.' separator between symbol and venue components
 *
 * 2026-08-13: "needs to be simpler. I need to be able to find the symbol for riding easily." Nobody
 * knows a symbol's MIC off the top of their head, and the venue is not a decision — it is a lookup. So
 * type the ticker, pick from what comes back, and the full id is filled in behind it.
 *
 * A complete `SYMBOL.VENUE` is still accepted verbatim, so anything already known still works.
 */
function DriverPicker({
  value,
  onChange,
  disabled,
}: {
  value: string;
  onChange: (instrumentId: string) => void;
  disabled?: boolean;
}) {
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);
  const query = text.trim().toUpperCase();
  const { data } = useQuery({
    queryKey: ["instrument-search", query],
    queryFn: ({ signal }) => searchInstruments(query, 6, signal),
    // Two characters is the point at which results start being useful rather than the whole universe.
    enabled: open && query.length >= 2,
    staleTime: 60_000,
    retry: false,
  });
  const matches = data?.results ?? [];

  if (value) {
    return (
      <div className="flex items-center gap-2">
        <span className="font-mono text-[11px] text-t1">{value.split(".")[0]}</span>
        <span className="font-mono text-[9px] text-t3">{value}</span>
        <button
          type="button"
          onClick={() => { onChange(""); setText(""); }}
          className="font-mono text-[10px] text-t3 underline hover:text-t2"
        >
          change
        </button>
      </div>
    );
  }

  return (
    <div className="relative">
      <input
        value={text}
        disabled={disabled}
        onChange={(e) => { setText(e.target.value.toUpperCase()); setOpen(true); }}
        onFocus={() => setOpen(true)}
        // Closes on blur and on Escape (codex review, Low). Without either, stale results sat open over
        // the rest of the detail panel after clicking or tabbing away. The blur is deferred a tick so a
        // click on a match still registers before the list unmounts.
        onBlur={() => setTimeout(() => setOpen(false), 120)}
        // A full id typed by hand still works — Enter accepts it as-is.
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            setOpen(false);
            return;
          }
          // Only a MIC-shaped id is accepted by hand — `BRK.B` is a real ticker with a dot in it and no
          // venue, and would otherwise reach the engine and leak the raw constructor error.
          if (e.key === "Enter" && /\.[A-Z]{3,}$/.test(query)) {
            onChange(query);
            setOpen(false);
          }
        }}
        placeholder="driver — type a ticker, e.g. SMH"
        className="w-full rounded border border-ds-line bg-ds-surf px-2 py-1 font-mono text-[16px] text-t1 placeholder:text-t3 sm:text-[10px]"
      />
      {open && matches.length > 0 && (
        <ul className="absolute z-20 mt-1 max-h-44 w-full overflow-y-auto rounded border border-ds-line2 bg-ds-surf shadow-lg">
          {matches.map((m) => (
            <li key={m.instrument_id}>
              <button
                type="button"
                onClick={() => { onChange(m.instrument_id); setOpen(false); }}
                className="flex w-full items-baseline gap-2 px-2 py-1 text-left hover:bg-ds-surf2"
              >
                <span className="font-mono text-[11px] font-bold text-t1">{m.symbol}</span>
                <span className="truncate font-mono text-[9px] text-t3">{m.name}</span>
                <span className="ml-auto font-mono text-[9px] text-t3">{m.venue}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function PositionDetailView({
  position: p,
  price,
  priorClose,
  cycle,
  onClose,
  onFlatten,
  pending,
  error,
  restingExits,
  queuedFlatten,
  stopReenter,
  peak,
  pyramid,
}: {
  position: PositionDTO;
  price: number | null;
  /** Prior session close, for today's move. From the shared `today_ranges` plane. */
  priorClose?: number | null;
  /** The trade cycle this position belongs to — carries state and cycle-total realized P&L. */
  cycle?: TradeDTO;
  onClose?: () => void;
  onFlatten?: () => void;
  pending?: boolean;
  error?: string | null;
  /** Quantity resting in orders that would REDUCE this position — a flatten is refused while any exist. */
  restingExits?: number | null;
  /** A flatten already deferred to the next open — NOT an order (no client_order_id, nothing at the broker
   *  yet); it only becomes one when the session opens and the queue replays it. */
  /** The deferred_flatten MANAGER watching this position, if one is armed — not an order (no
   *  client_order_id, nothing at the broker) until its trigger fires and it applies. */
  queuedFlatten?: { state: string; params: Record<string, unknown>; error?: string | null } | null;
  /** STOP-AND-REENTER (#47) — the currently-active manager in the watch→rearm chain for this position, if
   *  any, plus the toggle handler. `null` active = not armed (plain HOLDING). */
  stopReenter?: {
    active: Manager | null;
    onToggle: () => void;
    pending?: boolean;
    /** Why the last arm/cancel did not take. `null` = nothing to report, NOT "it worked" (#256). */
    error?: string | null;
  };
  /** PEAK (#46) — the currently-active `peak_watch` manager for this position, if any, plus the toggle
   *  handler. Unlike STOP-AND-REENTER, arming this ACTIVELY places an order (the initial wide trailing
   *  stop) — the toggle copy says so explicitly. `null` active = not armed (plain HOLDING, whatever
   *  protective stop already exists from entry is untouched). */
  peak?: {
    active: Manager | null;
    onToggle: () => void;
    pending?: boolean;
    /** Why the last arm/cancel did not take (#256). PEAK refuses when it cannot identify which protective
     *  stop is this position's own — a refusal the operator could not previously see. */
    error?: string | null;
  };
  /** PYRAMID (#38) — the currently-active `pyramid_watch` manager for this position, if any, plus the
   *  toggle handler. Lighter arm than PEAK: it does NOT place an order itself (works with the position's
   *  EXISTING protective stop), but it DOES need a driver symbol typed in before it can arm — no auto
   *  sector/driver mapping exists, so `onToggle` takes the driver instrument id the operator entered. */
  pyramid?: {
    active: Manager | null;
    onToggle: (driverInstrumentId: string) => void;
    pending?: boolean;
    /** Why the last arm did not take (#256). This is the one that mattered: PYRAMID has refused every arm
     *  it was ever given, silently, since it needs a protective stop the book has not had (#257). */
    error?: string | null;
  };
}) {
  // Pure presentational state (the driver symbol the operator is about to arm Pyramid with) — no command
  // logic here, matches this component's split of View (mockable in /dev/ui) vs. container.
  const [pyramidDriver, setPyramidDriver] = useState("");
  const long = p.side === "LONG";
  const { pct, amt } = computePnl(p, price);
  const strategy = strategyLabel(p.strategy_id);
  const symbol = p.instrument_id.split(".")[0];
  // ONE derivation of this position's direction-and-size, read by the market value, the day amount and
  // every quantity rendered below (#855). The local used to be called `signedQty` and to spell the rule
  // out itself; it is renamed so the imported predicate is not shadowed, and the rule now lives in one
  // module rather than in five hand-written copies.
  const signed = signedQty(p.side, p.quantity);
  const size = Math.abs(signed);
  // SIGNED, like the engine signs it (`engine_node.py:7441`, `d.market_value = last * signed`). A short
  // is a LIABILITY: reporting +$1,100 for one puts it on the wrong side of the account, and a book that
  // is net flat then reads as fully deployed — the WHD +28/-28 shape, in the UI this time.
  // Same three-state rule as `computePnl`: an unreadable quantity is unknown, and `magnitude`'s 0 is
  // for the sign rule, not for rendering an unknown as a confident $0.
  const mktValue = price != null && Number.isFinite(p.quantity) ? price * signed : null;
  const prior = priorClose ?? null;
  const dayAmt = price != null && prior != null ? signed * (price - prior) : null;
  // THE PERCENT POINTS THE SAME WAY AS THE DOLLARS BESIDE IT. This read `(price - prior) / prior`, the
  // SYMBOL's move, and printed it next to the POSITION's dollars — so a short whose mark rose rendered
  // "Day −$100  +10.00%", one number saying today hurt and the one touching it saying today helped.
  const dayPct = price != null && prior ? ((price - prior) / prior) * 100 * sideSign(p.side) : null;
  const cycleState = cycle?.state;

  return (
    <div className="flex h-[calc(100vh-9rem)] flex-col overflow-hidden rounded-xl border border-ds-line bg-ds-surf">
      <div className="flex items-center justify-between border-b border-ds-line px-4 py-2.5">
        <DetailIdentity symbol={symbol} venue={venueOf(p.instrument_id)}>
          <span
            className={`rounded px-1.5 py-0.5 font-mono text-[9px] font-semibold ${
              long ? "bg-status-bull/15 text-status-bull" : "bg-status-bear/15 text-status-bear"
            }`}
          >
            {p.side}
          </span>
          <span className="font-mono text-[11px] text-t2">
            {size} @ {p.avg_px_open.toFixed(2)}
          </span>
        </DetailIdentity>
        {onClose && <CloseButton onClick={onClose} />}
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-3">
        {/* Unrealized P&L — the headline; P&L tints the number, not the row */}
        <div className="mb-4">
          <div className="font-mono text-[10px] uppercase tracking-wide text-t3">Unrealized P&amp;L</div>
          <div className={`font-mono text-2xl font-bold ${pnlToneClass(amt)}`}>
            {amt != null ? `${amt >= 0 ? "+" : "−"}$${Math.abs(amt).toLocaleString(undefined, { maximumFractionDigits: 0 })}` : "—"}
            <span className="ml-2 text-sm">
              {amt != null ? `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%` : ""}
            </span>
          </div>
        </div>

        <div className="divide-y divide-ds-line/60">
          <Row label="Avg cost">{p.avg_px_open.toFixed(2)}</Row>
          <Row label="Quantity">{size}</Row>
          <Row label="Mark">{price != null ? price.toFixed(2) : "—"}</Row>
          {/* Today's move on this holding — qty × (mark − prior close), the same prior close the
              watchlist and portfolio read, so the same name never shows two different day changes. */}
          <Row label="Day">
            {dayAmt != null ? (
              <span className={pnlToneClass(dayAmt)}>
                {dayAmt >= 0 ? "+" : "−"}${Math.abs(dayAmt).toLocaleString(undefined, { maximumFractionDigits: 0 })}
                {dayPct != null && <span className="ml-1 text-t3">{dayPct >= 0 ? "+" : ""}{dayPct.toFixed(2)}%</span>}
              </span>
            ) : (
              "—"
            )}
          </Row>
          <Row label="Market value">
            {mktValue != null ? `$${mktValue.toLocaleString(undefined, { maximumFractionDigits: 0 })}` : "—"}
          </Row>
          <Row label="Realized">{cycle?.realized_pnl ?? p.realized_pnl}</Row>
          <Row label="Strategy">{strategy}</Row>
          {/* Cycle state is the cockpit's own projection (HELD/ARMED/WATCH) — it has no PositionDTO
              equivalent, and it is what tells you whether this holding is being managed or just sitting. */}
          {cycleState && <Row label="Cycle">{cycleState}</Row>}
          {/* A resting exit BLOCKS a flatten, so say it here rather than letting the operator discover it
              by sliding and being rejected. */}
          {restingExits != null && restingExits > 0 && (
            <Row label="Resting exits">
              <span className="text-status-bear">{restingExits}</span>
            </Row>
          )}
          {queuedFlatten && (
            <Row label="Flatten">
              <span className={queuedFlatten.state === "FAILED" ? "text-status-bear" : "text-status-info"}>
                {queuedFlatten.state === "FAILED" ? "queue failed" : "queued for the open"}
              </span>
            </Row>
          )}
          {stopReenter?.active && (
            <Row label="Stop &amp; re-enter">
              <span className={stopReenter.active.state === "FAILED" ? "text-status-bear" : "text-status-info"}>
                {stopReenter.active.state === "FAILED"
                  ? (stopReenter.active.error ?? "failed")
                  : stopReenter.active.kind === "stop_reenter_rearm"
                    ? "in rearm zone"
                    : "watching"}
              </span>
              {typeof stopReenter.active.params.rearm_count === "number" && stopReenter.active.params.rearm_count > 0 && (
                <span className="ml-1.5 text-t3">· re-entered ×{stopReenter.active.params.rearm_count}</span>
              )}
            </Row>
          )}
          {peak?.active && (
            <Row label="Peak">
              <span className={peak.active.state === "FAILED" ? "text-status-bear" : "text-status-info"}>
                {peak.active.state === "FAILED"
                  ? (peak.active.error ?? "failed")
                  : typeof peak.active.params.trim_count === "number" && peak.active.params.trim_count > 0
                    ? `trimmed ×${peak.active.params.trim_count}`
                    : peak.active.params.tightened
                      ? "tightened"
                      : "riding"}
              </span>
            </Row>
          )}
          {pyramid?.active && (
            <Row label="Pyramid">
              <span className={pyramid.active.state === "FAILED" ? "text-status-bear" : "text-status-info"}>
                {pyramid.active.state === "FAILED"
                  ? (pyramid.active.error ?? "failed")
                  : typeof pyramid.active.params.rung_count === "number" && pyramid.active.params.rung_count > 0
                    ? `added ×${pyramid.active.params.rung_count}`
                    : "riding"}
              </span>
            </Row>
          )}
        </div>

        {/* Stop & re-enter (#47) — a set-and-forget toggle, not an order: activating it does NOT place
            anything itself (every position already carries a resting protective stop as a platform-safety
            invariant, independent of this). It only watches for that stop firing, then manages the
            re-entry decision automatically (reclaim / base / floor-break-walk) — no per-trigger approval
            once armed (arming IS the confirmation, STYLE_GUIDE.md Layer 5). */}
        {stopReenter && (
          <div className="mt-4 rounded-lg border border-ds-line px-3 py-2">
            <div className="flex items-center justify-between gap-2">
              <span className="font-mono text-[10px] text-t2">
                Stop &amp; re-enter{" "}
                {stopReenter.active
                  ? "— buys back at the exit price or lower, never chases higher"
                  : "— off. Watches the next stop-out and rearms automatically."}
              </span>
              <button
                onClick={stopReenter.onToggle}
                disabled={stopReenter.pending}
                className={`shrink-0 rounded px-2 py-1 font-mono text-[10px] font-semibold transition-colors disabled:opacity-50 ${
                  stopReenter.active ? "bg-status-info/20 text-status-info" : "bg-ds-surf2 text-t2 hover:bg-ds-line2"
                }`}
              >
                {stopReenter.active ? "ON" : "OFF"}
              </button>
            </div>
            <ToggleError message={stopReenter.error} />
          </div>
        )}

        {/* PEAK (#46) — UNLIKE stop & re-enter, activating this DOES place an order: the initial wide
            trailing stop. It auto-cancels whatever protective stop already exists ONLY when identifiable as
            this position's own bracket-tagged stop; otherwise it refuses (surfaced via `error` on the
            manager row, e.g. "cancel it manually before arming PEAK") rather than guessing which order to
            touch. */}
        {peak && (
          <div className="mt-2 rounded-lg border border-ds-line px-3 py-2">
            <div className="flex items-center justify-between gap-2">
              <span className="font-mono text-[10px] text-t2">
                Peak{" "}
                {peak.active
                  ? "— adaptive trailing stop, tightens on a blowoff, trims into a confirmed fade"
                  : "— off. Arming places a wide trailing stop and replaces the existing one, if identifiable."}
              </span>
              <button
                onClick={peak.onToggle}
                disabled={peak.pending}
                className={`shrink-0 rounded px-2 py-1 font-mono text-[10px] font-semibold transition-colors disabled:opacity-50 ${
                  peak.active ? "bg-status-info/20 text-status-info" : "bg-ds-surf2 text-t2 hover:bg-ds-line2"
                }`}
              >
                {peak.active ? "ON" : "OFF"}
              </button>
            </div>
            <ToggleError message={peak.error} />
          </div>
        )}

        {/* PYRAMID (#38) — adds a tranche on a confirmed breakout, raises the trail with each add. Lighter
            arm than PEAK: does NOT place an order itself (works with the position's existing protective
            stop) — but DOES need a driver symbol (e.g. the sector ETF) typed in first; no auto sector-
            mapping exists yet. Toggle is disabled until a driver is entered. */}
        {pyramid && (
          <div className="mt-2 rounded-lg border border-ds-line px-3 py-2">
            <div className="flex items-center justify-between gap-2">
              <div className="flex min-w-0 flex-col gap-1">
                <span className="font-mono text-[10px] text-t2">
                  Pyramid{" "}
                  {pyramid.active
                    ? "— adds a tranche on a confirmed breakout, raises the trail with each add"
                    : "— off. Needs an existing protective stop (for R) and a driver symbol to arm."}
                </span>
                {!pyramid.active && (
                  <DriverPicker value={pyramidDriver} onChange={setPyramidDriver} disabled={pyramid.pending} />
                )}
              </div>
              <button
                onClick={() => pyramid.onToggle(pyramidDriver.trim())}
                disabled={pyramid.pending || (!pyramid.active && pyramidDriver.trim() === "")}
                className={`shrink-0 rounded px-2 py-1 font-mono text-[10px] font-semibold transition-colors disabled:opacity-50 ${
                  pyramid.active ? "bg-status-info/20 text-status-info" : "bg-ds-surf2 text-t2 hover:bg-ds-line2"
                }`}
              >
                {pyramid.active ? "ON" : "OFF"}
              </button>
            </div>
            <ToggleError message={pyramid.error} />
          </div>
        )}

        {/* Exit. A slide, not a tap — this places a real order that closes the position (STYLE_GUIDE:
            committing/destructive actions are slide gestures). The label states the exact consequence, and
            the engine re-sizes against its live position, so a position that moved is rejected rather than
            reversed. */}
        <div className="mt-5">
          {queuedFlatten && queuedFlatten.state !== "FAILED" ? (
            <div className="rounded-lg border border-ds-line bg-ds-surf2 px-3 py-2 font-mono text-[11px] text-t2">
              A flatten is queued — not an order yet, nothing has reached the broker. It submits as a market
              order the moment the session opens, re-checked against the live position at that instant.
            </div>
          ) : (
            <>
              <SlideToConfirm
                intent="sell"
                // "(404)" is a SHARE COUNT and reads exactly like an HTTP status — the operator asked whether
                // flatten was returning 404 when BETA simply held 404 shares. On a destructive control the
                // number must be unmistakably a quantity.
                label={`Slide to FLATTEN ${symbol} — ${long ? "sell" : "buy back"} ${size} shares`}
                pending={pending}
                reason={error ?? (queuedFlatten?.state === "FAILED" ? queuedFlatten.error : null)}
                onConfirm={() => onFlatten?.()}
              />
              <p className="mt-1.5 font-mono text-[10px] leading-relaxed text-t3">
                {long ? "Sells" : "Buys back"} {size}. In regular hours this is a market order; outside
                them it queues and submits at the next open. Cancel any resting exit orders first — the
                engine refuses otherwise.
              </p>
            </>
          )}
        </div>

        {/* Cycle state + protective stops are a trade-cycle projection (#73/#77) — not on the position yet. */}
        <div className="mt-4 rounded-lg border border-dashed border-ds-line px-3 py-2 font-mono text-[10px] text-t3">
          Protective stop/target display — pending #73/#77 (WorkingOrderDTO has no is_reduce_only yet, so a
          resting stop can't be told apart from an entry order unambiguously).
        </div>
      </div>
    </div>
  );
}

/** A manager from a CLOSED cycle must not mask a newer one on the position's CURRENT cycle (code review) —
 *  an old FAILED row from a prior open/close on the same instrument+strategy is a different intent than a
 *  fresh one just attached. `/managers` returns oldest-first, so within a single cycle an older FAILED row
 *  could otherwise out-rank a newer ARMED/APPLYING retry on the SAME cycle too — active always wins over
 *  failed before cycle recency is even considered. Priority: 1) active on the current cycle 2) FAILED on
 *  the current cycle 3) active on a legacy null cycle_id (pre-#68 migrated rows) 4) FAILED on a legacy null
 *  cycle_id. Shared by `deferred_flatten` and STOP-AND-REENTER (#47) — same cascade, two callers now. */
function pickActiveManager(candidates: Manager[], currentCycleId: string | null): Manager | undefined {
  const isActive = (m: Manager) => m.state === "ARMED" || m.state === "APPLYING";
  const onCurrentCycle = (m: Manager) => currentCycleId != null && m.cycle_id === currentCycleId;
  const onLegacyCycle = (m: Manager) => m.cycle_id == null;
  return (
    candidates.find((m) => onCurrentCycle(m) && isActive(m)) ??
    candidates.find((m) => onCurrentCycle(m) && !isActive(m)) ??
    candidates.find((m) => onLegacyCycle(m) && isActive(m)) ??
    candidates.find((m) => onLegacyCycle(m) && !isActive(m))
  );
}

export function PositionDetail({ focus }: DetailProps) {
  const closeDetail = useCockpitStore((s) => s.closeDetail);
  const beginCommand = useCockpitStore((s) => s.beginCommand);
  const endCommand = useCockpitStore((s) => s.endCommand);
  const qc = useQueryClient();
  const [commandId, setCommandId] = useState<string | null>(null);
  // A POST only ENQUEUES; the engine's accept/reject arrives on the ack (#39). Never report an exit as done
  // on the enqueue — a rejected flatten must say why, not close the screen and look like it worked.
  // The LONGER budget, because a flatten cancels resting orders before it acts and the ack is only
  // written when that whole sequence finishes (#269). At the default 8s the ack expired mid-flight and
  // resolved to `unknown`, which reports nothing — an exit that had already filled looked like a button
  // that did nothing at all.
  const ack = useCommandStatus(commandId, CANCEL_THEN_ACT_TIMEOUT_MS);
  // Separate command/ack tracking for the STOP-AND-REENTER toggle (#47) — independent of the flatten
  // command above; conflating the two would show a flatten's pending/error state on the toggle or vice
  // versa.
  const [stopReenterCommandId, setStopReenterCommandId] = useState<string | null>(null);
  const stopReenterAck = useCommandStatus(stopReenterCommandId, CANCEL_THEN_ACT_TIMEOUT_MS);
  // Same for PEAK (#46) — its own toggle, its own ack, independent of flatten and stop-reenter above.
  const [peakCommandId, setPeakCommandId] = useState<string | null>(null);
  const peakAck = useCommandStatus(peakCommandId, CANCEL_THEN_ACT_TIMEOUT_MS);
  // Same for PYRAMID (#38) — its own toggle, its own ack.
  const [pyramidCommandId, setPyramidCommandId] = useState<string | null>(null);
  const pyramidAck = useCommandStatus(pyramidCommandId, CANCEL_THEN_ACT_TIMEOUT_MS);
  const key = focus.kind === "position" ? focus.positionKey : "";
  const instrumentId = focus.kind === "position" ? focus.instrumentId : "";
  // The TRADE-CYCLE plane first: it carries cycle state (HELD/ARMED/WATCH), cycle_id and the cycle's
  // realized P&L, none of which exist on PositionDTO. The flat positions source is the fallback, for the
  // legacy PortfolioTile and for anything the projection hasn't picked up.
  const { data: tradeData } = useSource("trades", {});
  const { data } = useSource("positions", {});
  const { price, bars, todayRange } = useInstrument(instrumentId);
  const cycle = ((tradeData as { trades?: TradeDTO[] } | undefined)?.trades ?? []).find(
    (t) => `${t.strategy_id}:${t.instrument_id}` === key && t.state === "HELD",
  );
  const flatten = useMutation({
    mutationFn: (f: FlattenPayload) => flattenPosition(f),
    onSuccess: (res) => setCommandId(res.command_id),
    // A POST that never returns a command_id leaves nothing to wait on, so the busy marker must come off
    // here or the row stays PROCESSING with no ack that will ever clear it.
    onError: () => endCommand(key),
  });

  // The ack for the original enqueue expires long before an overnight manager fires, so a screen re-opened
  // hours later has to read the durable manager list directly rather than trust a stale/expired command_id.
  const { data: managerData } = useQuery({
    queryKey: ["managers"],
    queryFn: getManagers,
    staleTime: 5_000,
    refetchInterval: 15_000,
  });

  // EVERY terminal state clears the busy marker, including `unknown`. A marker that only cleared on
  // `accepted` would leave the row PROCESSING forever whenever an ack was lost — and the next real
  // flatten would then be invisible against a stale one that never ended.
  useEffect(() => {
    if (ack.state === "pending") return;
    endCommand(key);
  }, [ack.state, key, endCommand]);

  useEffect(() => {
    if (ack.state !== "accepted") return;
    // The position is closing (or a manager was just attached); the blotter/positions plane and the
    // manager list are authoritative from here.
    qc.invalidateQueries({ queryKey: ["positions"] });
    qc.invalidateQueries({ queryKey: ["trades"] });
    qc.invalidateQueries({ queryKey: ["orders"] });
    qc.invalidateQueries({ queryKey: ["managers"] });
  }, [ack.state, qc]);

  useEffect(() => {
    if (stopReenterAck.state !== "accepted") return;
    qc.invalidateQueries({ queryKey: ["managers"] });
  }, [stopReenterAck.state, qc]);

  useEffect(() => {
    if (peakAck.state !== "accepted") return;
    qc.invalidateQueries({ queryKey: ["managers"] });
    // Arming PEAK places a real order (unlike stop-reenter) — the blotter/positions plane needs to hear
    // about it too, same as flatten's own ack effect above.
    qc.invalidateQueries({ queryKey: ["positions"] });
    qc.invalidateQueries({ queryKey: ["orders"] });
  }, [peakAck.state, qc]);

  useEffect(() => {
    if (pyramidAck.state !== "accepted") return;
    // Unlike PEAK, arming Pyramid does NOT place an order itself (it works with the position's existing
    // stop) — only the manager row changed, so only invalidate that, same lighter footprint as stop-reenter.
    qc.invalidateQueries({ queryKey: ["managers"] });
  }, [pyramidAck.state, qc]);

  // Hooks — MUST stay above the `if (!position) return` guard below (rules-of-hooks: a component that
  // returns early on one render and not another must call the exact same hooks either way, or React
  // throws "rendered fewer/more hooks than expected" — caught live, browser-verify, #47).
  const attach = useMutation({
    mutationFn: attachManager,
    onSuccess: (res) => setStopReenterCommandId(res.command_id),
  });
  const cancel = useMutation({
    mutationFn: cancelManager,
    // Same ack-driven invalidation as attach (via `stopReenterAck`'s effect above), NOT an immediate
    // invalidate here — a POST only means ENQUEUED, the engine processes the cancel asynchronously (browser-
    // verify caught this: invalidating on the POST response alone refetched too early, before the cancel
    // had actually landed, and the query's 15s poll interval meant the UI stayed stale for a while after).
    onSuccess: (res) => setStopReenterCommandId(res.command_id),
  });
  const peakAttach = useMutation({
    mutationFn: attachManager,
    onSuccess: (res) => setPeakCommandId(res.command_id),
  });
  const peakCancel = useMutation({
    mutationFn: cancelManager,
    onSuccess: (res) => setPeakCommandId(res.command_id), // ack-driven invalidate, same reasoning as above
  });
  const pyramidAttach = useMutation({
    mutationFn: attachManager,
    onSuccess: (res) => setPyramidCommandId(res.command_id),
  });
  const pyramidCancel = useMutation({
    mutationFn: cancelManager,
    onSuccess: (res) => setPyramidCommandId(res.command_id), // ack-driven invalidate, same reasoning as above
  });

  const { data: orderData } = useSource("orders", {});
  // PEAK's arm params come from the `peak` settings domain, not a hardcoded constant. `ready` gates
  // the toggle so a position can never be armed with fallback numbers the operator has since changed.
  const { params: peakParams, ready: peakReady, error: peakSettingsError } = usePeakParams();
  const positions = (data as PositionsResponse | undefined)?.positions ?? [];
  const position =
    positions.find((p) => positionKey(p) === key) ??
    // Synthesised from the cycle so the detail still renders when only the projection knows about it.
    (cycle && cycle.avg_px_open != null
      ? ({
          instrument_id: cycle.instrument_id,
          strategy_id: cycle.strategy_id,
          side: cycle.side,
          // MAGNITUDE (#855). This synthesised row is what the whole screen then reads — the four
          // command payloads included — so a sign copied in here would travel all the way to the
          // engine, and `flatten.py:93` refuses an `expected_qty` that is not the live quantity.
          quantity: magnitude(cycle.quantity),
          avg_px_open: cycle.avg_px_open,
          realized_pnl: cycle.realized_pnl,
        } as PositionDTO)
      : undefined);

  // Which manager is active for each toggle, derived ONCE (codex review, round 5). A second, looser
  // derivation used to decide when to drop a stale command id, and the two could disagree: a FAILED row
  // from a PRIOR cycle made the looser one report "armed" while the toggle rendered OFF for the current
  // cycle, which unlocked the toggle mid-flight and allowed exactly the duplicate arm this is meant to
  // prevent. One derivation cannot disagree with itself.
  //
  // `position` is optional here only because these feed hooks, which must run before the `if (!position)`
  // guard below — same rules-of-hooks note as further up.
  const currentCycleId = cycle?.cycle_id ?? null;
  const managersFor = (...kinds: string[]) =>
    (managerData?.managers ?? []).filter(
      (m) =>
        kinds.includes(m.kind) &&
        m.instrument_id === position?.instrument_id &&
        m.strategy_id === position?.strategy_id &&
        (m.state === "ARMED" || m.state === "APPLYING" || m.state === "FAILED"),
    );
  const activeStopReenter =
    pickActiveManager(managersFor("stop_reenter_watch", "stop_reenter_rearm"), currentCycleId) ?? null;
  const activePeak = pickActiveManager(managersFor("peak_watch"), currentCycleId) ?? null;
  const activePyramid = pickActiveManager(managersFor("pyramid_watch"), currentCycleId) ?? null;

  // The durable manager list is the authority; an ack only stands in until it catches up. Keyed on
  // PRESENCE, not on manager_id: a chain that hands off to a successor stays armed throughout, and keying
  // on identity would clear a still-pending OFF command mid-handoff and swallow its rejection.
  //
  // This is what unlocks a toggle stuck on a lost ack. `togglePending` keeps it locked on `unknown`,
  // because that is a local 8s timeout and says nothing about whether the engine ran the command — a
  // second press would arm a duplicate manager, which is how FIG collected duplicates on 2026-08-11. Once
  // the manager list proves what happened, the ack is moot and its command id goes. A genuine rejection
  // arms nothing, so nothing changes here and the reason stays on screen until the operator presses again.
  const stopReenterArmed = activeStopReenter !== null;
  const peakArmed = activePeak !== null;
  const pyramidArmed = activePyramid !== null;
  useEffect(() => setStopReenterCommandId(null), [stopReenterArmed]);
  useEffect(() => setPeakCommandId(null), [peakArmed]);
  useEffect(() => setPyramidCommandId(null), [pyramidArmed]);

  if (!position) {
    return (
      <div className="flex h-[calc(100vh-9rem)] flex-col items-center justify-center rounded-xl border border-ds-line bg-ds-surf text-t2">
        <p className="font-mono text-sm">position not found</p>
        <button onClick={closeDetail} className="mt-3 rounded-md bg-ds-surf2 px-3 py-1.5 font-mono text-xs text-t2">
          close
        </button>
      </div>
    );
  }

  // A manager from a CLOSED cycle must not mask a newer one on the position's CURRENT cycle (code review) —
  // an old FAILED deferred_flatten from a prior open/close on the same instrument+strategy is a different
  // intent than a fresh one just attached. `/managers` returns oldest-first, so within a single cycle an
  // older FAILED row could otherwise out-rank a newer ARMED/APPLYING retry on the SAME cycle too (code
  // review) — active always wins over failed before cycle recency is even considered. Priority:
  // 1) active (ARMED/APPLYING) on the current cycle  2) FAILED on the current cycle
  // 3) active on a legacy null cycle_id (pre-#68 migrated rows)  4) FAILED on a legacy null cycle_id
  const managerCandidates = (managerData?.managers ?? []).filter(
    (m) =>
      m.kind === "deferred_flatten" &&
      m.instrument_id === position.instrument_id &&
      m.strategy_id === position.strategy_id &&
      (m.state === "ARMED" || m.state === "APPLYING" || m.state === "FAILED"),
  );
  const queuedFlatten = pickActiveManager(managerCandidates, currentCycleId);

  // Orders that would REDUCE this position: a LONG is reduced by SELLs, a SHORT by BUYs. Only these block a
  // flatten, and only for this instrument + strategy.
  const reducingSide = position.side === "LONG" ? "SELL" : "BUY";
  const restingExits = ((orderData as { orders?: OrderDTO[] } | undefined)?.orders ?? [])
    .filter(
      (o) =>
        o.instrument_id === position.instrument_id &&
        o.side === reducingSide &&
        (o.status === "ACCEPTED" || o.status === "WORKING" || o.status === "PARTIALLY_FILLED"),
    )
    .reduce((sum, o) => sum + (o.leaves_qty ?? 0), 0);

  const levels = lastLevels(bars);

  return (
    <PositionDetailView
      position={position}
      price={price}
      priorClose={todayRange?.prevClose ?? null}
      cycle={cycle}
      onClose={closeDetail}
      restingExits={restingExits}
      queuedFlatten={queuedFlatten ?? null}
      stopReenter={{
        active: activeStopReenter,
        pending: togglePending(stopReenterCommandId, stopReenterAck, attach, cancel),
        error: toggleError(stopReenterAck, attach, cancel),
        onToggle: () => {
          attach.reset();
          cancel.reset();
          setStopReenterCommandId(null);
          if (activeStopReenter) {
            cancel.mutate(activeStopReenter.manager_id);
            return;
          }
          const defaults = stopReenterDefaults(position.side as "LONG" | "SHORT", price, levels);
          attach.mutate({
            kind: "stop_reenter_watch",
            instrument_id: position.instrument_id,
            strategy_id: position.strategy_id,
            cycle_id: cycle?.cycle_id ?? null,
            leash: "AUTO",
            params: {
              expected_side: position.side,
              qty: magnitude(position.quantity),
              rearm_count: 0,
              rearm_max: 3,
              ...defaults,
            },
          });
        },
      }}
      peak={{
        active: activePeak,
        // Settings gate ARMING only. Turning PEAK OFF must never depend on them — a cancel needs no
        // parameters, and a slow or failing settings call would otherwise leave the operator unable to
        // disarm a manager that is actively selling. (codex review, round 2, High.)
        pending: (!activePeak && !peakReady) || togglePending(peakCommandId, peakAck, peakAttach, peakCancel, { durablyOn: peakArmed }),
        // An engine rejection is the more actionable of the two and wins. The settings error only shows
        // when there is nothing else to say AND it is actually blocking something — i.e. not while PEAK
        // is already armed, where settings are irrelevant. (codex review, round 2, Medium.)
        error: toggleError(peakAck, peakAttach, peakCancel, { durablyOn: peakArmed }) ?? (activePeak ? null : peakSettingsError),
        onToggle: () => {
          // Both mutations and the previous ack are cleared BEFORE branching, so the line under the
          // toggle can only ever describe the press that is happening now (codex review, High + Low).
          peakAttach.reset();
          peakCancel.reset();
          setPeakCommandId(null);
          if (activePeak) {
            peakCancel.mutate(activePeak.manager_id);
            return;
          }
          peakAttach.mutate({
            kind: "peak_watch",
            instrument_id: position.instrument_id,
            strategy_id: position.strategy_id,
            cycle_id: cycle?.cycle_id ?? null,
            leash: "AUTO",
            params: {
              expected_side: position.side,
              qty: magnitude(position.quantity),
              ...peakParams,
            },
          });
        },
      }}
      pyramid={{
        active: activePyramid,
        pending: togglePending(pyramidCommandId, pyramidAck, pyramidAttach, pyramidCancel),
        error: toggleError(pyramidAck, pyramidAttach, pyramidCancel),
        onToggle: (driverInstrumentId: string) => {
          pyramidAttach.reset();
          pyramidCancel.reset();
          setPyramidCommandId(null);
          if (activePyramid) {
            pyramidCancel.mutate(activePyramid.manager_id);
            return;
          }
          if (!driverInstrumentId) return; // toggle button is disabled in this case too — defense in depth
          pyramidAttach.mutate({
            kind: "pyramid_watch",
            instrument_id: position.instrument_id,
            strategy_id: position.strategy_id,
            cycle_id: cycle?.cycle_id ?? null,
            leash: "AUTO",
            params: {
              expected_side: position.side,
              qty: magnitude(position.quantity),
              driver_instrument_id: driverInstrumentId,
              ...pyramidDefaults(),
            },
          });
        },
      }}
      pending={flatten.isPending || (commandId !== null && ack.state === "pending")}
      error={
        flatten.isError
          ? String(flatten.error)
          : ack.state === "rejected"
            ? (ack.error ?? "the engine rejected this exit")
            : ack.state === "unknown"
              ? "no answer from the engine — check the position before retrying"
              : null
      }
      onFlatten={() => {
        setCommandId(null); // a retry waits on its OWN ack, never the previous one
        // Marked busy BEFORE the POST, not in `onSuccess`. The enqueue itself is a network round trip, and
        // the whole complaint was that pressing the control produced no visible change — starting the
        // feedback only once the server answered would reproduce it in miniature.
        beginCommand(key, "flatten", `Flattening ${position.instrument_id.split(".")[0]}`, Date.now());
        flatten.mutate({
          instrument_id: position.instrument_id,
          strategy_id: position.strategy_id,
          expected_side: position.side,
          expected_qty: magnitude(position.quantity),
          cycle_id: cycle?.cycle_id ?? null,
        });
      }}
    />
  );
}
