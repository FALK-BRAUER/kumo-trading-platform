/**
 * WS multiplex protocol — TS mirror of the backend's structured envelope (api/models.py: Topic,
 * ControlFrame, EventFrame). Hand-written (not OpenAPI-generated) because WS frames are invisible to
 * OpenAPI; keeping them here decouples the live plane from the REST codegen.
 *
 * Wire (locked #7 P3): client→server `control`; server→client `event` routed by `topic`.
 */
export type Channel =
  | "positions"
  | "trades"
  | "external_activity"
  | "bars"
  | "fills"
  | "prices"
  | "orders"
  | "account"
  | "risk"
  | "quotes"
  | "vwaps"
  | "today_ranges"
  | "fundamentals"
  | "session"
  | "equity_curve"
  //: The rotation read (#384) — the engine grades ETF ratio axes off our own bars and publishes it on
  //: its timer, like every other plane here. It was absent from this union because the payload used to
  //: be a file the api read off disk, so there was nothing to subscribe to.
  | "rotation";
export type FrameType = "snapshot" | "bar" | "fill" | "status";

export interface Topic {
  channel: Channel;
  params?: Record<string, string>;
}

/**
 * The `session` frame (#212) — the STRATEGY's own state, as opposed to the account's. Carries the
 * lifecycle, the latest decision with its per-symbol reasons, recent journal rows and the exit
 * trail. Shape mirrors `backend/api/session_state.py`; only the fields the UI reads are typed.
 */
export interface SessionTrail {
  symbol: string;
  entry: number | null;
  peak: number | null;
  /** Quantity the TRAIL believes is held. Can lag the broker — see `trailLines`. */
  qty?: number | null;
  sessions_held: number | null;
  /**
   * `adopted` means the PEAK WAS NEVER OBSERVED — the position predates any usable record, so
   * peak-relative exits are inert for it (#197 B1). Anything drawn from that peak would be fiction,
   * which is exactly what the column exists to prevent.
   */
  quality: "live" | "reconstructed" | "adopted" | string;
}

export interface SessionFrame {
  strategy_id?: string;
  session?: string | null;
  today?: string | null;
  decision_is_today?: boolean;
  decision?: { summary?: string; reasons?: Record<string, string> } | null;
  trail?: SessionTrail[];
}

export interface ControlFrame {
  type: "control";
  op: "subscribe" | "unsubscribe" | "ping";
  topic?: Topic;
}

export interface EventFrame {
  type: "event";
  event: "data" | "status" | "error" | "control_ack" | "heartbeat";
  topic?: Topic;
  payload: Record<string, unknown>;
}

/**
 * Canonical topic key — the client refcount + query-cache identity. MUST match the backend's
 * `Topic.key()`: channel, then sorted `k=v` params joined by `&`.
 */
export function canonicalTopicKey(topic: Topic): string {
  const params = topic.params ?? {};
  const keys = Object.keys(params).sort();
  if (keys.length === 0) return topic.channel;
  return `${topic.channel}:${keys.map((k) => `${k}=${params[k]}`).join("&")}`;
}
