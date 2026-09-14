"use client";

/**
 * StrategyOrderForm (#51) — the STRATEGY-FIRST order ticket. Strategy is the first selection and scopes the
 * visible fields (the operator's contract). v1 = MANUAL only (the sole live strategy); MOMENTUM / ETF_AUTO are shown
 * disabled ("soon"). The MANUAL body is assisted: pick an entry mechanism + a stop mechanism + a risk %, and
 * the levels/size PREFILL from the catalog (#65) — recovering the assistance the retired OrderModal had, in a
 * compact, tile-friendly form. Every prefilled field stays hand-editable; downstream target + shares always
 * recompute from the EFFECTIVE (displayed) entry/stop, never stale prefill.
 *
 * Submits through the shared order-core (buildOrderPayload/buildBracketPayload + the #39 ack flow), same as
 * the vanilla tile. Strategy is MANUAL on the wire (the only live strategy) — no strategy_id field yet.
 */
import { useEffect, useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { submitBracket, submitOrder, type BracketPayload, type OrderPayload } from "@/lib/api/client";
import { SlideToConfirm } from "@/components/ds/SlideToConfirm";
import { marketOrderWouldBeCanceled } from "@/lib/market";
import {
  bracketGeoOk,
  bracketTif,
  protectionExpiresForTif,
  buildBracketPayload,
  buildOrderPayload,
  stopTriggerOk,
  type Action,
  type OrderType,
} from "@/lib/order/payload";
import { AUTOFILL_DEFAULTS, profileFromAutofill, type StopModel } from "@/config/orderTicket";
import { STRATEGIES } from "@/config/strategies";
import { computePrefill, roundTick, sizePosition, spreadPct } from "@/lib/prefill";
import { useAccount } from "@/lib/framework/account";
import { useInstrument } from "@/lib/framework/instrument";
import { useCommandStatus } from "@/lib/framework/useCommandStatus";

type EntryMech = "now" | "limit" | "pullback" | "breakout";
type StopMech = "ichimoku" | "atr" | "percent" | "manual";

// Entry seg → catalog mechanism id + broker order type. Breakout is a stop-ENTRY (trigger), which brackets
// can't carry (bracket entries are market|limit only) → breakout submits a plain stop order, no bracket.
const ENTRY_CFG: Record<EntryMech, { label: string; mechId: string; orderType: OrderType; editable: boolean; bracketable: boolean }> = {
  now: { label: "Now", mechId: "marketable", orderType: "market", editable: false, bracketable: true },
  limit: { label: "Limit", mechId: "marketable", orderType: "limit", editable: true, bracketable: true },
  pullback: { label: "Pullback", mechId: "kijun_pullback", orderType: "limit", editable: true, bracketable: true },
  breakout: { label: "Breakout", mechId: "breakout", orderType: "stop", editable: true, bracketable: false },
};
const ENTRY_ORDER: EntryMech[] = ["now", "limit", "pullback", "breakout"];
const STOP_ORDER: StopMech[] = ["ichimoku", "atr", "percent", "manual"];
// Catalog comes from @/config/strategies — this list used to be duplicated here and again in the transfer
// UI, so a strategy going live had to be remembered in three places.

const fmt = (n: number | null): string => (n != null ? String(n) : "");
const parse = (s: string): number => (parseFloat(s) > 0 ? parseFloat(s) : 0);

/** Current US-equity session in ET: pre (04:00–09:30), open (09:30–16:00), post (16:00–20:00), else closed.
 *  Drives the premarket/after-hours entry affordance — extended sessions trade only on a DAY limit. */
function usSession(): "pre" | "open" | "post" | "closed" {
  const p = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(new Date());
  const wd = p.find((x) => x.type === "weekday")?.value ?? "";
  if (wd === "Sat" || wd === "Sun") return "closed";
  let hh = Number(p.find((x) => x.type === "hour")?.value ?? "0");
  if (hh === 24) hh = 0;
  const t = hh * 60 + Number(p.find((x) => x.type === "minute")?.value ?? "0");
  if (t >= 4 * 60 && t < 9 * 60 + 30) return "pre";
  if (t >= 9 * 60 + 30 && t < 16 * 60) return "open";
  if (t >= 16 * 60 && t < 20 * 60) return "post";
  return "closed";
}

interface Fields {
  entry: string;
  stop: string;
  target: string;
  qty: string;
  touched: Set<"entry" | "stop" | "target" | "qty">;
}
const FRESH: Fields = { entry: "", stop: "", target: "", qty: "", touched: new Set() };

export function StrategyOrderForm({ instrumentId }: { instrumentId: string }) {
  const [strategy, setStrategy] = useState("MANUAL");
  const [action, setAction] = useState<Action>("BUY");
  const [entryMech, setEntryMech] = useState<EntryMech>("now");
  const [stopMech, setStopMech] = useState<StopMech>("ichimoku");
  const [riskStr, setRiskStr] = useState(String(AUTOFILL_DEFAULTS.riskFraction * 100)); // 0.5 (%)
  // Extended-hours (pre/post-market): Alpaca fills these ONLY on a DAY limit — never a market or bracket.
  // Toggling on coerces the order to a limit and drops the bracket (see the derivation below).
  const [extHours, setExtHours] = useState(false);
  const [f, setF] = useState<Fields>(FRESH);
  // Session-aware default: in a pre/post-market session, land on the extended-hours limit (a market order
  // would just be canceled) and start the entry on a limit so the price field is ready. Computed once on
  // mount; the user can still toggle back to a regular order (e.g. to rest a limit for the open).
  const [session] = useState(usSession);
  const extEligible = session === "pre" || session === "post";
  const extLabel = session === "pre" ? "Premarket" : session === "post" ? "After-hrs" : "Ext hrs";
  useEffect(() => {
    if (extEligible) {
      setExtHours(true);
      setEntryMech("limit");
    }
    // mount-only: seed the default; never fight a later manual toggle.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const { bars, price, quote } = useInstrument(instrumentId);
  const { account } = useAccount();
  const equity = account?.equity ?? null;
  const buyingPower = account?.buying_power ?? null;

  const entryCfg = ENTRY_CFG[entryMech];
  const stopModel: StopModel = stopMech === "manual" ? "percent" : stopMech; // manual → user types stop; percent is a harmless base
  const riskFrac = (parseFloat(riskStr) || 0) / 100;
  const targetR = AUTOFILL_DEFAULTS.targetR;

  const profile = useMemo(() => {
    const base = profileFromAutofill(AUTOFILL_DEFAULTS, stopModel);
    return { ...base, sizing: { ...base.sizing, riskMode: "fraction_of_equity" as const, riskFraction: riskFrac } };
  }, [stopModel, riskFrac]);

  const prefill = useMemo(
    () =>
      computePrefill({
        action,
        profile,
        stopModel,
        entryMechanism: { id: entryCfg.mechId, params: {} },
        bars,
        last: price,
        mid: quote?.mid ?? null,
        spreadPct: spreadPct(quote),
        equity,
        buyingPower,
      }),
    [action, profile, stopModel, entryCfg.mechId, bars, price, quote, equity, buyingPower],
  );

  // Prefill the BASE legs (entry, stop) into any field the user hasn't touched. Deps are the prefill
  // primitives, so this runs only when a suggestion changes — not every render (no loop). Target + shares are
  // NOT filled here; they derive from the EFFECTIVE entry/stop below so a manual edit propagates downstream.
  useEffect(() => {
    setF((prev) => {
      const next = { ...prev };
      if (!prev.touched.has("entry")) next.entry = entryCfg.editable ? fmt(prefill.entry) : "";
      if (!prev.touched.has("stop")) next.stop = stopMech === "manual" ? prev.stop : fmt(prefill.stop);
      return next;
    });
  }, [prefill.entry, prefill.stop, entryCfg.editable, stopMech]);

  // Effective (displayed) values — the single source of truth for sizing, target, validation, submit.
  const effEntry = entryCfg.editable ? parse(f.entry) : price ?? 0; // market entry sizes off the live price
  const effStop = parse(f.stop);
  const derivedTarget =
    effEntry > 0 && effStop > 0
      ? roundTick(action === "BUY" ? effEntry + targetR * Math.abs(effEntry - effStop) : effEntry - targetR * Math.abs(effEntry - effStop))
      : null;
  const effTarget = f.touched.has("target") ? parse(f.target) : derivedTarget ?? 0;
  const sized =
    effEntry > 0 && effStop > 0
      ? sizePosition(effEntry, effStop, action, profile.sizing, equity, buyingPower)
      : { shares: 0, riskUsd: null as number | null, notes: [] as string[] };
  const effShares = f.touched.has("qty") ? parseInt(f.qty) || 0 : sized.shares;

  // Extended hours forces a limit (Alpaca fills pre/post-market only on a DAY limit) and drops the bracket
  // (bracket/OCO legs are regular-hours only). Otherwise the effective type is the entry mechanism's type.
  const effOrderType: OrderType = extHours ? "limit" : entryCfg.orderType;

  // Bracket = auto-attach the protective stop + target, but only when the entry type can carry one and both
  // levels exist. Breakout (stop-entry) is always a plain order; extended hours can't bracket at all.
  const useBracket = !extHours && entryCfg.bracketable && effStop > 0 && effTarget > 0;
  const entryRef = effOrderType === "limit" && effEntry > 0 ? effEntry : price ?? 0;

  // Validation — mirrors the vanilla tile's geometry safety.
  const triggerOk = effOrderType !== "stop" || (effEntry > 0 && stopTriggerOk(action, "stop", effEntry, price ?? 0));
  const priceOk = effOrderType === "market" || effEntry > 0;
  const bracketOk = !useBracket || bracketGeoOk(action, entryRef, effStop, effTarget);
  // A market order (incl. a market-entry bracket) placed while the market is closed is canceled by the broker
  // in seconds — it can't rest. Block it and steer to limit/stop (#113). Limit/stop entries are never blocked.
  const marketBlocked = marketOrderWouldBeCanceled(effOrderType, Date.now());
  const canSubmit = effShares > 0 && priceOk && triggerOk && bracketOk && !marketBlocked;

  const [commandId, setCommandId] = useState<string | null>(null);
  const order = useMutation({
    mutationFn: (v: { kind: "order"; p: OrderPayload } | { kind: "bracket"; p: BracketPayload }) =>
      v.kind === "bracket" ? submitBracket(v.p) : submitOrder(v.p),
    onSuccess: (data) => setCommandId(data.command_id),
  });
  const { state: cmdState, error: cmdError } = useCommandStatus(commandId);

  function submit() {
    if (!canSubmit) return;
    if (useBracket) {
      order.mutate({
        kind: "bracket",
        p: buildBracketPayload({
          instrumentId,
          action,
          orderType: entryCfg.orderType === "limit" ? "limit" : "market",
          priceNum: effEntry,
          sharesNum: effShares,
          stopNum: effStop,
          targetNum: effTarget,
          // NOT "day" (#315). Alpaca hands the parent's TIF to both legs, so this field decides how long
          // the PROTECTIVE STOP lives — a DAY bracket leaves the position naked from the close until the
          // #239 backstop's first regular-hours tick.
          tif: bracketTif(entryCfg.orderType === "limit" ? "limit" : "market"),
        }),
      });
      return;
    }
    order.mutate({
      kind: "order",
      p: buildOrderPayload({ instrumentId, action, orderType: effOrderType, extended: extHours, priceNum: effEntry, tif: "day", sharesNum: effShares }),
    });
  }

  const terminal = cmdState === "accepted" || cmdState === "rejected" || cmdState === "unknown";
  function newOrder() {
    order.reset();
    setCommandId(null);
    setF(FRESH);
  }
  const enqueued = order.isSuccess;

  const set = (field: "entry" | "stop" | "target" | "qty", v: string) =>
    setF((prev) => ({ ...prev, [field]: v, touched: new Set(prev.touched).add(field) }));
  // Switching a mechanism re-suggests that leg (drop the user's touch on it).
  const untouch = (field: "entry" | "stop") =>
    setF((prev) => {
      const t = new Set(prev.touched);
      t.delete(field);
      return { ...prev, touched: t };
    });

  const seg = (on: boolean, disabled = false) =>
    `px-2 py-0.5 rounded font-mono text-[11px] transition-colors ${
      disabled ? "cursor-not-allowed bg-ds-surf text-t3" : on ? "bg-status-info/20 text-t1 ring-1 ring-status-info/40" : "bg-ds-surf2 text-t2 hover:bg-ds-line2"
    }`;
  // 16px on mobile stops iOS auto-zoom-on-focus (now that page pinch-zoom is allowed again); 12px on desktop.
  const numField = "w-24 rounded border border-ds-line2 bg-ds-surf2 px-2 py-1 font-mono text-[16px] text-t1 outline-none focus:border-status-info sm:text-xs";

  return (
    <div className="flex h-full flex-col gap-2 overflow-auto p-3">
      {/* Strategy — first, always. */}
      <div className="flex items-center gap-1.5">
        <span className="text-[9px] uppercase text-t3">Strategy</span>
        {STRATEGIES.map((s) => (
          <button
            key={s.id}
            onClick={() => s.live && setStrategy(s.id)}
            disabled={!s.live}
            title={s.live ? undefined : "coming soon"}
            className={`px-2 py-0.5 rounded font-mono text-[11px] font-bold transition-colors ${
              !s.live ? "cursor-not-allowed bg-ds-surf text-t3" : strategy === s.id ? "bg-status-bull/15 text-status-bull ring-1 ring-status-bull/40" : "bg-ds-surf2 text-t2 hover:bg-ds-surf2"
            }`}
          >
            {s.id}
            {!s.live ? " ·soon" : ""}
          </button>
        ))}
      </div>

      <div className="flex items-baseline justify-between border-t border-ds-line pt-2">
        <span className="font-mono text-sm font-bold text-t1">{instrumentId.split(".")[0]}</span>
        <span className="font-mono text-[11px] text-t2">
          {strategy}
          {price != null ? ` · $${price.toFixed(2)}` : ""}
        </span>
      </div>

      {/* Side */}
      <div className="flex gap-1.5">
        {(["BUY", "SELL"] as Action[]).map((a) => (
          <button
            key={a}
            onClick={() => setAction(a)}
            className={`px-3 py-0.5 rounded font-mono text-[11px] font-bold transition-colors ${
              action === a ? (a === "BUY" ? "bg-status-bull/15 text-status-bull ring-1 ring-status-bull/40" : "bg-status-bear/15 text-status-bear ring-1 ring-status-bear/40") : "bg-ds-surf2 text-t2 hover:bg-ds-surf2"
            }`}
          >
            {a}
          </button>
        ))}
      </div>

      {/* Entry mechanism — Now (market) and Breakout (stop) are invalid for extended hours (limit-only). */}
      <div className="flex items-center gap-1.5">
        <span className="w-10 text-[9px] uppercase text-t3">Entry</span>
        {ENTRY_ORDER.map((m) => {
          const disabled = extHours && (m === "now" || m === "breakout");
          return (
            <button
              key={m}
              disabled={disabled}
              title={disabled ? "not available in extended hours (limit only)" : undefined}
              onClick={() => { if (disabled) return; setEntryMech(m); untouch("entry"); }}
              className={seg(entryMech === m, disabled)}
            >
              {ENTRY_CFG[m].label}
            </button>
          );
        })}
      </div>

      {/* Session — extended-hours (pre/post-market) toggle. Forces a DAY limit, drops the bracket. */}
      <div className="flex items-center gap-1.5">
        <span className="w-10 text-[9px] uppercase text-t3">Session</span>
        <button
          onClick={() =>
            setExtHours((on) => {
              const next = !on;
              // Ext hours needs a limit price; jump off a market/stop entry so the price field appears.
              if (next && (entryMech === "now" || entryMech === "breakout")) {
                setEntryMech("limit");
                untouch("entry");
              }
              return next;
            })
          }
          className={seg(extHours)}
        >
          {extLabel}
        </button>
        <span className="text-[10px] text-t3">
          {extHours
            ? `${extEligible ? "trades now" : "pre/post-market"} · DAY limit · no bracket`
            : extEligible
              ? `${session === "pre" ? "premarket" : "after-hours"} open — tap to trade now`
              : "regular hours"}
        </span>
      </div>

      {/* Stop mechanism */}
      <div className="flex items-center gap-1.5">
        <span className="w-10 text-[9px] uppercase text-t3">Stop</span>
        {STOP_ORDER.map((m) => (
          <button key={m} onClick={() => { setStopMech(m); untouch("stop"); }} className={seg(stopMech === m)}>
            {m === "ichimoku" ? "Ichi" : m === "percent" ? "%" : m.toUpperCase()}
          </button>
        ))}
      </div>

      {/* Risk → sizing */}
      <div className="flex items-center gap-2">
        <span className="w-10 text-[9px] uppercase text-t3">Risk</span>
        <input value={riskStr} onChange={(e) => setRiskStr(e.target.value)} inputMode="decimal" className="w-16 rounded border border-ds-line2 bg-ds-surf2 px-2 py-1 font-mono text-[16px] text-t1 outline-none focus:border-status-info sm:text-xs" />
        <span className="text-[11px] text-t3">%</span>
        <span className="ml-auto font-mono text-[11px] text-t2">
          {sized.shares > 0 && effEntry > 0 ? `${effShares} sh · ~$${(effShares * effEntry / 1000).toFixed(1)}k` : "—"}
          {sized.riskUsd != null && effShares > 0 ? ` · risk ~$${Math.round(sized.riskUsd)}` : ""}
        </span>
      </div>

      {/* Level chips — compact, editable */}
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-0.5">
          <span className="text-[9px] uppercase text-t3">Entry {entryCfg.editable ? "" : "(mkt)"}</span>
          {entryCfg.editable ? (
            <input value={f.entry} onChange={(e) => set("entry", e.target.value)} inputMode="decimal" className={numField} />
          ) : (
            <span className="w-24 px-2 py-1 font-mono text-xs text-t2">~${(price ?? 0).toFixed(2)}</span>
          )}
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="text-[9px] uppercase text-t3">Stop</span>
          <input value={f.stop} onChange={(e) => set("stop", e.target.value)} inputMode="decimal" className={numField} />
        </label>
        {entryCfg.bracketable && (
          <label className="flex flex-col gap-0.5">
            <span className="text-[9px] uppercase text-t3">Target</span>
            <input value={f.touched.has("target") ? f.target : fmt(derivedTarget)} onChange={(e) => set("target", e.target.value)} inputMode="decimal" className={numField} />
          </label>
        )}
        <label className="flex flex-col gap-0.5">
          <span className="text-[9px] uppercase text-t3">Qty</span>
          <input value={f.touched.has("qty") ? f.qty : String(sized.shares || "")} onChange={(e) => set("qty", e.target.value)} inputMode="numeric" className={numField} />
        </label>
      </div>

      {prefill.notes.length > 0 && <div className="font-mono text-[10px] text-t3">{prefill.notes.join(" · ")}</div>}

      {marketBlocked && !terminal && (
        <div className="rounded border border-status-warn/40 bg-status-warn/10 px-2 py-1.5 font-mono text-[10px] text-status-warn">
          Market closed — a market order will be canceled by the broker. Use a <b>Limit</b> or <b>Stop</b> entry to rest until open.
        </div>
      )}

      {terminal ? (
        <button
          onClick={newOrder}
          className="mt-auto w-full rounded bg-ds-surf2 py-1.5 font-mono text-xs font-bold text-t1 transition-colors hover:bg-ds-line2"
        >
          New order →
        </button>
      ) : (
        // Committing a live order slides — a tap must never place it (#108).
        <SlideToConfirm
          className="mt-auto"
          label={`${action}${extHours ? ` ${extLabel.toLowerCase()}` : useBracket ? " bracket" : entryCfg.orderType === "stop" ? " stop" : ""} ${effShares || ""} ${instrumentId.split(".")[0]}`.trim()}
          intent={action === "SELL" ? "sell" : "buy"}
          disabled={!canSubmit}
          pending={order.isPending || enqueued}
          reason={enqueued ? "Awaiting engine…" : undefined}
          onConfirm={submit}
        />
      )}
      {/* Say it BEFORE the slide, not after the fill (#315). Alpaca gives both bracket legs the parent's
          TIF, so a limit entry buys its own expiry with the stop's: the protection dies at 16:00 and the
          position is naked until the #239 backstop's first regular-hours tick. A market entry has no such
          trade-off and rests GTC. Silence here is what let a DAY stop read as permanent protection. */}
      {useBracket && protectionExpiresForTif(bracketTif(entryCfg.orderType === "limit" ? "limit" : "market")) && (
        <div className="font-mono text-[10px] leading-snug text-status-watch">
          Protective stop expires at the close — Alpaca gives both bracket legs the entry&apos;s
          time-in-force, and a resting limit entry cannot be GTC. The backstop re-covers at the next open.
        </div>
      )}
      {cmdState === "accepted" && <div className="font-mono text-[10px] text-status-bull">Placed ✓</div>}
      {cmdState === "rejected" && <div className="font-mono text-[10px] text-status-bear">Rejected{cmdError ? ` — ${cmdError}` : ""}</div>}
      {cmdState === "unknown" && <div className="font-mono text-[10px] text-status-watch">No engine ack — check the Orders tab.</div>}
      {order.isError && <div className="font-mono text-[10px] text-status-bear">Failed — {String(order.error)}</div>}
    </div>
  );
}
