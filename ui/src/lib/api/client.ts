/**
 * Thin typed REST client. Types come from the generated schema — this file only wires fetch.
 */
import { API_BASE } from "@/lib/config";
import { normaliseQuantity } from "./normaliseQuantity";
import type { InstrumentSearchResponse, PositionsResponse, WatchlistResponse } from "./types";

export async function getPositions(): Promise<PositionsResponse> {
  const res = await fetch(`${API_BASE}/positions`, { cache: "no-store" });
  if (!res.ok) {
    throw new Error(`GET /positions failed: ${res.status}`);
  }
  return normaliseQuantity((await res.json()) as PositionsResponse);
}

// --- Orders (MANUAL lane, #32/#5) ---
export interface OrderPayload {
  instrument_id: string;
  side: "BUY" | "SELL";
  quantity: number;
  order_type: "market" | "limit" | "stop";
  price?: number;
  trigger_price?: number;
  time_in_force?: string; // day | gtc | opg (on-open) | cls (on-close)
  extended_hours?: boolean; // pre/post-market — backend enforces limit-only
}

/** Command correlation (#39). A POST returns `ok` = ENQUEUED (not placed) + a `command_id`; poll
 *  `getCommandStatus(command_id)` for the engine's accept/reject. `client_order_id` correlates the order
 *  lifecycle (Orders blotter). */
export interface CommandResult {
  ok: boolean;
  command_id: string;
  client_order_id?: string;
}
/** UI-side command state: the backend returns pending|accepted|rejected; the CALLER maps its own poll
 *  timeout → `unknown` (a lost ack must never read as a false reject). */
export type CommandState = "pending" | "accepted" | "rejected" | "unknown";
export interface CommandStatus {
  command_id: string;
  status: "pending" | "accepted" | "rejected";
  command_type?: string | null;
  error?: string | null;
}

/** The engine's ack for a command (#39). `pending` until the engine acks; then `accepted` (took it —
 *  Orders blotter is authoritative after) or `rejected` (with `error`). */
export async function getCommandStatus(commandId: string): Promise<CommandStatus> {
  const res = await fetch(`${API_BASE}/commands/${encodeURIComponent(commandId)}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`GET /commands failed: ${res.status}`);
  return res.json();
}

/** Submit a discretionary order → the command layer. `ok` = enqueued (not placed). Only PLACED if the engine
 *  is armed (KUMO_ORDERS_ARMED); poll getCommandStatus(command_id) for accept/reject. */
export async function submitOrder(order: OrderPayload): Promise<CommandResult> {
  const res = await fetch(`${API_BASE}/orders`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(order),
  });
  if (!res.ok) throw new Error(`POST /orders failed: ${res.status}`);
  return res.json();
}

// --- Brackets (#34) ---
export interface BracketPayload {
  instrument_id: string;
  side: "BUY" | "SELL";
  quantity: number;
  entry_order_type: "market" | "limit";
  price?: number; // entry limit price (limit entry)
  stop_trigger: number; // protective stop — required
  target_price: number; // take-profit — required (native Nautilus bracket needs entry+stop+target)
  time_in_force?: string;
}

/** Submit a bracketed entry (#34): entry + protective stop (+ optional target). The engine arms the
 *  protective legs on the entry fill (OTO) and OCO-links them. Placed only if the engine is armed. */
export async function submitBracket(b: BracketPayload): Promise<CommandResult> {
  const res = await fetch(`${API_BASE}/orders/bracket`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(b),
  });
  if (!res.ok) throw new Error(`POST /orders/bracket failed: ${res.status}`);
  return res.json();
}

/** Cancel a working order (#33 blotter) → cancel_order command. Gated by KUMO_ORDERS_ARMED on the engine. */
export async function cancelOrder(clientOrderId: string): Promise<CommandResult> {
  const res = await fetch(`${API_BASE}/orders/${encodeURIComponent(clientOrderId)}/cancel`, { method: "POST" });
  if (!res.ok) throw new Error(`cancel order failed: ${res.status}`);
  return res.json();
}

/** Modify a working order's qty/price/trigger (#33 blotter) → modify_order command. */
export async function modifyOrder(
  clientOrderId: string,
  body: { quantity?: number; price?: number; trigger_price?: number },
): Promise<CommandResult> {
  const res = await fetch(`${API_BASE}/orders/${encodeURIComponent(clientOrderId)}/modify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`modify order failed: ${res.status}`);
  return res.json();
}

/** Ask the backend to stream this symbol on demand (viewing a search result that isn't on the watchlist).
 *  Fire-and-forget — the detail surface shows "loading" until the engine's reconcile picks it up. */
export async function requestStream(instrumentId: string): Promise<void> {
  await fetch(`${API_BASE}/instruments/${encodeURIComponent(instrumentId)}/stream`, {
    method: "POST",
  }).catch(() => {});
}

// --- Watchlist (runtime, Postgres-backed) ---
export async function getWatchlist(): Promise<WatchlistResponse> {
  const res = await fetch(`${API_BASE}/watchlist`, { cache: "no-store" });
  if (!res.ok) throw new Error(`GET /watchlist failed: ${res.status}`);
  return (await res.json()) as WatchlistResponse;
}

export async function addToWatchlist(instrumentId: string): Promise<WatchlistResponse> {
  const res = await fetch(`${API_BASE}/watchlist`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instrument_id: instrumentId }),
  });
  if (!res.ok) throw new Error(`POST /watchlist failed: ${res.status}`);
  return (await res.json()) as WatchlistResponse;
}

export async function removeFromWatchlist(instrumentId: string): Promise<WatchlistResponse> {
  const res = await fetch(`${API_BASE}/watchlist/${encodeURIComponent(instrumentId)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`DELETE /watchlist failed: ${res.status}`);
  return (await res.json()) as WatchlistResponse;
}

// --- Settings framework (#49) ---
/** A minimal JSON Schema shape — enough to render a flat settings form (enum/number/bool/string). */
export interface JsonSchema {
  title?: string;
  description?: string;
  type?: string;
  properties?: Record<string, JsonSchemaField>;
}
export interface JsonSchemaField {
  type?: string;
  enum?: string[];
  default?: unknown;
  title?: string;
  description?: string;
  minimum?: number;
  maximum?: number;
}
export type SettingsValues = Record<string, unknown>;

export async function listSettings(): Promise<{ domains: string[] }> {
  const res = await fetch(`${API_BASE}/settings`, { cache: "no-store" });
  if (!res.ok) throw new Error(`GET /settings failed: ${res.status}`);
  return res.json();
}

export async function getSettings(domain: string): Promise<{ schema: JsonSchema; values: SettingsValues }> {
  const res = await fetch(`${API_BASE}/settings/${encodeURIComponent(domain)}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`GET /settings/${domain} failed: ${res.status}`);
  return res.json();
}

/** Persist a domain's values. Throws with the per-field validation errors on a 422 (invalid → not saved). */
export async function putSettings(domain: string, values: SettingsValues): Promise<{ values: SettingsValues }> {
  const res = await fetch(`${API_BASE}/settings/${encodeURIComponent(domain)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
  if (res.status === 422) {
    const body = await res.json().catch(() => ({}));
    throw new Error(`Invalid: ${JSON.stringify(body.detail ?? body)}`);
  }
  if (!res.ok) throw new Error(`PUT /settings/${domain} failed: ${res.status}`);
  return res.json();
}

/** Thrown when the backend reports search is unavailable (503) — provider isn't Alpaca / catalog not loaded.
 *  The tile catches this to show a distinct "search unavailable" state rather than a generic error. */
export class SearchUnavailableError extends Error {}

export async function searchInstruments(
  q: string,
  limit = 20,
  signal?: AbortSignal,
): Promise<InstrumentSearchResponse> {
  const params = new URLSearchParams({ q, limit: String(limit) });
  const res = await fetch(`${API_BASE}/instruments/search?${params}`, { cache: "no-store", signal });
  if (res.status === 503) {
    throw new SearchUnavailableError("instrument search unavailable");
  }
  if (!res.ok) {
    throw new Error(`GET /instruments/search failed: ${res.status}`);
  }
  return (await res.json()) as InstrumentSearchResponse;
}

// --- Internal position transfers (#80 spin-off) ---
export interface TransferPayload {
  instrument_id: string;
  target_strategy_id: string;
  quantity: number;
  side?: string;
  source_strategy_id?: string;
  /** CARRY_OVER: basis migrates, nothing realized. MARKET: source crystallizes P&L, target starts clean. */
  pricing_mode?: "MARKET" | "CARRY_OVER";
  reason_code?: string;
}

export interface Transfer {
  transfer_id: string;
  instrument_id: string;
  source_strategy_id: string;
  target_strategy_id: string;
  side: string;
  quantity: number;
  pricing_mode: "MARKET" | "CARRY_OVER";
  transfer_px: number;
  source_avg_px: number;
  state: "PREPARED" | "SOURCE_APPLIED" | "DEST_APPLIED" | "COMPLETED" | "FAILED";
  reason_code: string;
  error?: string | null;
}

/** Move a position (or part of one) between strategies. Places NO order — the broker net is unchanged and
 *  only the owning strategy changes. `ok` = enqueued; poll getCommandStatus for accept/reject. */
export async function transferPosition(t: TransferPayload): Promise<CommandResult> {
  const res = await fetch(`${API_BASE}/transfers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(t),
  });
  if (!res.ok) throw new Error(`POST /transfers failed: ${res.status}`);
  return res.json();
}

/** Transfers and their outbox state. Anything not COMPLETED/FAILED is in flight or awaiting recovery. */
export async function getTransfers(): Promise<{ transfers: Transfer[] }> {
  const res = await fetch(`${API_BASE}/transfers`, { cache: "no-store" });
  if (!res.ok) throw new Error(`GET /transfers failed: ${res.status}`);
  // A transfer row carries `side` + `quantity` like any position-shaped row (#855).
  return normaliseQuantity(await res.json());
}

// --- Flatten a position (#170 first slice) ---
export interface FlattenPayload {
  instrument_id: string;
  strategy_id: string;
  /** What the operator SAW — the engine rejects if the live position no longer matches. */
  expected_side: string;
  expected_qty?: number;
  /** The cycle the operator was looking at when they confirmed. If the position's cycle changes while a
   *  deferred flatten sits queued, the manager refuses rather than applying against a coincidental match. */
  cycle_id?: string | null;
}

/** Close a position, long or short. `ok` = enqueued; the engine sizes the close from its LIVE position and
 *  rejects a stale one, so poll getCommandStatus for the real outcome. Outside regular hours this attaches a
 *  `deferred_flatten` manager instead of placing an order immediately — see getManagers. */
export async function flattenPosition(f: FlattenPayload): Promise<CommandResult> {
  const res = await fetch(`${API_BASE}/positions/flatten`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(f),
  });
  if (!res.ok) throw new Error(`POST /positions/flatten failed: ${res.status}`);
  return res.json();
}

// --- Managers (#55) — the generic attach/watch/trigger/act automation framework ---
export interface Manager {
  manager_id: string;
  kind: string;
  instrument_id: string;
  strategy_id: string;
  /** Null for pre-#68 migrated rows. A stale manager from a closed cycle must not mask a newer one on the
   *  position's current cycle — match on this before falling back to instrument/strategy alone. */
  cycle_id: string | null;
  leash: "AUTO" | "CONFIRM" | "ALERT";
  state: "ARMED" | "PROPOSED" | "APPROVED" | "APPLYING" | "APPLIED" | "FAILED" | "CANCELLED";
  params: Record<string, unknown>;
  /** Populated only when state is FAILED — why. */
  error?: string | null;
  /** When the row was written / last changed (#400). Optional because the API only began returning them
   *  in #400 — a build served by an older engine sends neither, and `null` there means UNKNOWN, which
   *  must not be rendered as a date. For a terminal row (FAILED/CANCELLED) `updated_at` is when it died,
   *  because nothing touches it afterwards. */
  created_at?: string | null;
  updated_at?: string | null;
}

/** Every manager instance, whatever its kind or state. The ack for the original attach command expires long
 *  before an overnight manager fires, so a screen re-opened hours later reads this instead. */
export async function getManagers(): Promise<{ managers: Manager[] }> {
  const res = await fetch(`${API_BASE}/managers`, { cache: "no-store" });
  if (!res.ok) throw new Error(`GET /managers failed: ${res.status}`);
  return res.json();
}

export interface AttachManagerPayload {
  kind: string;
  instrument_id: string;
  strategy_id?: string | null;
  cycle_id?: string | null;
  // AUTO only, and the type says so rather than letting a caller compile something the engine refuses
  // (#255). Arming IS the confirmation — there is no per-action approve step (#47) — and the engine's
  // `mg.claim` has no leash predicate, so a CONFIRM/ALERT row would be applied exactly like AUTO. The
  // ManagerDTO's own `leash` stays wide, because a stored row must remain readable whatever it holds.
  leash?: "AUTO";
  params: Record<string, unknown>;
}

/** Arm a manager (#55/#47) — e.g. `stop_reenter_watch` on a HELD position. `ok` = ENQUEUED, not armed yet;
 *  poll `getCommandStatus` for the engine's accept/reject (a mismatched position, disarmed orders, or an
 *  unknown `kind` reject there). */
export async function attachManager(m: AttachManagerPayload): Promise<CommandResult> {
  const res = await fetch(`${API_BASE}/managers/attach`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(m),
  });
  if (!res.ok) throw new Error(`POST /managers/attach failed: ${res.status}`);
  return res.json();
}

/** Deactivate an armed manager (#55/#47) — the toggle's OFF path. A no-op if it already reached a terminal
 *  state (APPLIED/FAILED/CANCELLED); never overwrites a real outcome. */
export async function cancelManager(managerId: string): Promise<CommandResult> {
  const res = await fetch(`${API_BASE}/managers/${managerId}/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  if (!res.ok) throw new Error(`POST /managers/${managerId}/cancel failed: ${res.status}`);
  return res.json();
}

// --- Symbol pool (#79 follow-on) ---
// The pool MOMENTUM ranks each session, plus the operator whitelist/blacklist. Lives in the cockpit
// so the lists are managed in the same place as everything else, not a second app on another port.
export interface PoolEntry {
  symbol: string;
  sources: string[];
  provenance: string;
  meta: Record<string, unknown>;
  held: boolean;
  override: "pin" | "exclude" | null;
  reason: string | null;
}

export interface PoolSource {
  name: string;
  status: string;
  symbol_count: number;
  refreshed_at: string | null;
  stale: boolean;
  detail: string | null;
}

export interface PoolResponse {
  count: number;
  symbols: PoolEntry[];
  sources: PoolSource[];
}

export async function getPool(): Promise<PoolResponse> {
  const res = await fetch(`${API_BASE}/pool`, { cache: "no-store" });
  if (!res.ok) {
    throw new Error(`GET /pool failed: ${res.status}`);
  }
  return (await res.json()) as PoolResponse;
}

export async function setPoolOverride(body: {
  symbol: string;
  kind: "pin" | "exclude";
  reason?: string;
}): Promise<PoolResponse> {
  const res = await fetch(`${API_BASE}/pool/override`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(`POST /pool/override failed: ${res.status}`);
  }
  return (await res.json()) as PoolResponse;
}

export async function clearPoolOverride(symbol: string, kind: "pin" | "exclude"): Promise<PoolResponse> {
  const res = await fetch(`${API_BASE}/pool/override/${encodeURIComponent(symbol)}/${kind}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    throw new Error(`DELETE /pool/override failed: ${res.status}`);
  }
  return (await res.json()) as PoolResponse;
}

// --- Market rotation (#351) ---
export interface RotationWindow {
  est?: number | null;
  lo?: number | null;
  hi?: number | null;
  sig?: boolean | null;
  prev?: number | null;
  move?: number | null;
  n?: number | null;
}

export interface RotationAxis {
  group: string;
  label: string;
  pair: string;
  meaning?: string | null;
  verdict?: string | null;
  note?: string | null;
  adx?: number | null;
  num: string;
  den: string;
  win?: Record<string, RotationWindow> | null;
  /** The weekly gate's own reading — ABV / IN / BLW. The route passes the payload through unchanged,
   *  so these were always on the wire; the tile simply never declared them and could not show them. */
  wk?: string | null;
  d_pos?: string | null;
  d_abv?: boolean | null;
  d_TgtK?: boolean | null;
  accel?: number | null;
  err?: string | null;
}

export interface RotationResponse {
  generated: string | null;
  source: string | null;
  axes: RotationAxis[];
  errors: unknown[];
  /** Non-null when the payload is missing or unreadable — absence is not an empty rotation (#351). */
  error: string | null;
}

export async function getRotation(): Promise<RotationResponse> {
  const res = await fetch(`${API_BASE}/market/rotation`, { cache: "no-store" });
  if (!res.ok) {
    throw new Error(`GET /market/rotation failed: ${res.status}`);
  }
  return (await res.json()) as RotationResponse;
}
