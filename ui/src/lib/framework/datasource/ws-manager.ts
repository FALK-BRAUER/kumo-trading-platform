/**
 * WS manager — the live data plane, OUTSIDE React (TanStack Query owns REST only). One multiplexed
 * socket, ref-counted per-topic subscriptions, reconnect + backoff, resubscribe-all on reopen. React
 * reads it through `useSyncExternalStore` (wired in 3c); this module touches no React.
 *
 * Per-topic lifecycle mirrors the backend gate: subscribe → control_ack → replay_start → snapshot →
 * replay_end → increments. Snapshot replaces `data`; increments append into the snapshot's list.
 */
import { WS_BASE } from "@/lib/config";
import { normaliseQuantity } from "@/lib/api/normaliseQuantity";
import {
  canonicalTopicKey,
  type ControlFrame,
  type EventFrame,
  type FrameType,
  type Topic,
} from "./protocol";

export type ConnectionState = "disconnected" | "connecting" | "connected";
export type ReplayPhase = "idle" | "replaying" | "live";

export interface TopicState {
  refCount: number;
  subscribed: boolean;
  replayPhase: ReplayPhase;
  data: unknown; // snapshot payload, with increments appended
  error?: { code?: string; message?: string };
}

type Listener = () => void;

/** frame_type → the snapshot list key its increments append into. */
const INCREMENT_KEY: Partial<Record<FrameType, string>> = { bar: "bars", fill: "fills" };
const WS_URL = `${WS_BASE}/ws/stream`;
const MAX_BACKOFF_MS = 10_000;

interface Entry extends TopicState {
  topic: Topic;
}

function snapshot(e: Entry): TopicState {
  return {
    refCount: e.refCount,
    subscribed: e.subscribed,
    replayPhase: e.replayPhase,
    data: e.data,
    error: e.error,
  };
}

class WsManager {
  private ws: WebSocket | null = null;
  private connection: ConnectionState = "disconnected";
  private topics = new Map<string, Entry>();
  // Immutable per-topic views — useSyncExternalStore needs a stable ref that only changes on change.
  private views = new Map<string, TopicState>();
  private listeners = new Set<Listener>();
  private backoff = 500;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  /** Subscribe a topic (ref-counted). Returns an unsubscribe fn for the binding tile to call. */
  subscribe(topic: Topic): () => void {
    const key = canonicalTopicKey(topic);
    let entry = this.topics.get(key);
    if (!entry) {
      entry = { topic, refCount: 0, subscribed: false, replayPhase: "idle", data: undefined };
      this.topics.set(key, entry);
    }
    entry.refCount += 1;
    if (entry.refCount === 1) {
      if (this.connection === "connected") this.sendSubscribe(topic);
      else this.connect();
    }
    this.emit();
    return () => this.unsubscribe(topic);
  }

  /** True if any topic is currently referenced — so a WS drop is a real problem, not an idle view (#26). */
  hasActiveSubscriptions(): boolean {
    for (const entry of this.topics.values()) if (entry.refCount > 0) return true;
    return false;
  }

  getTopicState(key: string): TopicState | undefined {
    return this.views.get(key);
  }

  getConnection(): ConnectionState {
    return this.connection;
  }

  /** Register a change listener (for useSyncExternalStore). */
  onChange(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private unsubscribe(topic: Topic): void {
    const key = canonicalTopicKey(topic);
    const entry = this.topics.get(key);
    if (!entry) return;
    entry.refCount -= 1;
    if (entry.refCount <= 0) {
      if (this.connection === "connected") this.send({ type: "control", op: "unsubscribe", topic });
      this.topics.delete(key);
    }
    this.emit();
  }

  private emit(): void {
    // Rebuild immutable views before notifying so getTopicState returns a fresh ref on real change
    // (and a stable one between emits — required by useSyncExternalStore).
    for (const [key, entry] of this.topics) this.views.set(key, snapshot(entry));
    for (const key of [...this.views.keys()]) if (!this.topics.has(key)) this.views.delete(key);
    for (const listener of this.listeners) listener();
  }

  private connect(): void {
    if (this.connection !== "disconnected") return;
    if (typeof WebSocket === "undefined") return; // SSR / non-browser guard
    this.connection = "connecting";
    const ws = new WebSocket(WS_URL);
    this.ws = ws;

    ws.onopen = () => {
      this.connection = "connected";
      this.backoff = 500;
      // Resubscribe every still-referenced topic; reset their replay state for a fresh snapshot.
      for (const entry of this.topics.values()) {
        if (entry.refCount > 0) {
          entry.subscribed = false;
          entry.replayPhase = "idle";
          entry.error = undefined;
          this.sendSubscribe(entry.topic);
        }
      }
      this.emit();
    };
    ws.onmessage = (ev: MessageEvent<string>) => this.onMessage(ev.data);
    ws.onclose = () => this.onDrop();
    ws.onerror = () => this.onDrop();
  }

  private onDrop(): void {
    this.ws = null;
    this.connection = "disconnected";
    for (const entry of this.topics.values()) {
      entry.subscribed = false;
      entry.replayPhase = "idle";
    }
    this.emit();
    if ([...this.topics.values()].some((e) => e.refCount > 0)) this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) return;
    const delay = Math.min(this.backoff, MAX_BACKOFF_MS) + Math.random() * 250;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.backoff = Math.min(this.backoff * 2, MAX_BACKOFF_MS);
      this.connect();
    }, delay);
  }

  private sendSubscribe(topic: Topic): void {
    this.send({ type: "control", op: "subscribe", topic });
  }

  private send(frame: ControlFrame): void {
    this.ws?.send(JSON.stringify(frame));
  }

  private onMessage(raw: string): void {
    let frame: EventFrame;
    try {
      frame = JSON.parse(raw) as EventFrame;
    } catch {
      return;
    }
    if (frame.event === "heartbeat" || !frame.topic) return;
    const entry = this.topics.get(canonicalTopicKey(frame.topic));
    if (!entry) return;

    const payload = frame.payload as {
      frame_type?: FrameType;
      data?: unknown;
      op?: string;
      status?: string;
      error?: { code?: string; message?: string };
    };

    switch (frame.event) {
      case "control_ack":
        if (payload.op === "subscribe") {
          if (payload.status === "ok") entry.subscribed = true;
          else {
            entry.error = payload.error;
            entry.replayPhase = "live";
          }
        }
        break;
      case "status": {
        const code = (payload.data as { code?: string } | undefined)?.code;
        if (code === "replay_start") entry.replayPhase = "replaying";
        else if (code === "replay_end") entry.replayPhase = "live";
        break;
      }
      case "data":
        if (payload.frame_type === "snapshot") {
          // The WS decode point (#855). Increments are bars and FILLS, and a fill DOES carry a
          // quantity — `ChartTile` renders it. Fills are exempt because an order fill quantity is a
          // Nautilus `Quantity`, which cannot be negative, and its direction is a BUY/SELL rather
          // than a LONG/SHORT. See `ROW_KEYS` in `normaliseQuantity.ts` for the same statement.
          entry.data = normaliseQuantity(payload.data);
        } else if (payload.frame_type && INCREMENT_KEY[payload.frame_type]) {
          const listKey = INCREMENT_KEY[payload.frame_type] as string;
          const cur = (entry.data as Record<string, unknown[]> | undefined) ?? {};
          const list = Array.isArray(cur[listKey]) ? cur[listKey] : [];
          entry.data = { ...cur, [listKey]: [...list, payload.data] };
        }
        break;
      case "error":
        entry.error = (payload.data as { details?: { code?: string; message?: string } } | undefined)?.details ?? {
          code: (payload.data as { code?: string } | undefined)?.code,
        };
        break;
    }
    this.emit();
  }
}

/** Process-wide singleton — one socket for the whole board. */
export const wsManager = new WsManager();
