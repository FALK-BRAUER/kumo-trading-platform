"use client";

/**
 * RefreshBar — a thin countdown bar showing the live-feed heartbeat (mirrors the old cockpit's poll bar).
 * Drains full→empty over the push cadence, looping while the WS is connected; goes red when the socket
 * is down. Cadence must match the backend's LIVE_PUSH_INTERVAL (api/app.py).
 */
import { useSyncExternalStore } from "react";
import { wsManager } from "@/lib/framework/datasource/ws-manager";

const REFRESH_SECONDS = 1; // keep in sync with backend LIVE_PUSH_INTERVAL

export function RefreshBar() {
  const connection = useSyncExternalStore(
    (cb) => wsManager.onChange(cb),
    () => wsManager.getConnection(),
    () => "disconnected" as const,
  );
  // Only a real transport problem when the socket is down WHILE something is subscribed. An idle view with
  // no WS subscriber is neutral — not red (#26: the old bar lied, going red on every no-subscriber view).
  const active = useSyncExternalStore(
    (cb) => wsManager.onChange(cb),
    () => wsManager.hasActiveSubscriptions(),
    () => false,
  );
  const live = connection === "connected";
  const problem = active && !live;

  return (
    <div className="mb-3 h-px w-full overflow-hidden rounded-full bg-ds-surf2">
      <style>{`@keyframes cockpit-poll-drain { from { width: 100%; } to { width: 0%; } }`}</style>
      {live ? (
        <div
          className="h-px bg-status-bull/60"
          style={{ animation: `cockpit-poll-drain ${REFRESH_SECONDS}s linear infinite` }}
        />
      ) : problem ? (
        <div className="h-px w-full bg-status-bear/50" title="live stream disconnected" />
      ) : (
        <div className="h-px w-full bg-ds-surf2/40" title="idle" />
      )}
    </div>
  );
}
