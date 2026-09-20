"use client";

/**
 * "This page is running an old build" (#345 item 5).
 *
 * A phone served a 10-minute-old bundle showing NET·all +$847.61 when the deployed answer was +$250.64.
 * Wrong money, indistinguishable from a live render — the same class as #336/#343, except the defect is
 * in DELIVERY, so no correctness in the engine can prevent it. The operator debugs from that phone.
 *
 * POLLED, not fetched once. A tab left open across a deploy is the exact case: the bundle was fresh when
 * it loaded and goes stale under the operator's feet without a single interaction. A one-shot check at
 * mount would answer the one moment the answer is always "fresh".
 *
 * `no-store` on the request as well as on the response, because a cached answer would be exactly as
 * stale as the bundle it is meant to catch.
 */
import { useEffect, useState } from "react";
import { buildFreshness, type BuildFreshness } from "@/lib/buildFreshness";

/** Slow on purpose: a deploy is a minutes-scale event and this must not add traffic on a phone. */
const POLL_MS = 60_000;

export function StaleBundleBanner() {
  const [state, setState] = useState<BuildFreshness>({ kind: "unknown", reason: "not checked yet" });

  useEffect(() => {
    let alive = true;
    const running = process.env.NEXT_PUBLIC_BUILD_ID ?? null;

    async function check() {
      try {
        const res = await fetch("/api/build", { cache: "no-store" });
        const body = (await res.json()) as { buildId?: string | null };
        if (alive) setState(buildFreshness(running, body?.buildId));
      } catch {
        // A failed check is UNKNOWN, never stale. An offline phone must not be told its code is old.
        if (alive) setState({ kind: "unknown", reason: "build check unreachable" });
      }
    }

    void check();
    const id = setInterval(check, POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  // Silent unless it is actually stale. `unknown` shows nothing: a dev build has no id, and a banner
  // that appears on every local run is one nobody reads in production.
  if (state.kind !== "stale") return null;

  return (
    <button
      type="button"
      onClick={() => window.location.reload()}
      title={`Running build ${state.running}; the server is serving ${state.serving}. Numbers on this page may be from an earlier deploy.`}
      className="w-full bg-status-bear/15 px-3 py-1.5 text-center font-mono text-[11px] font-semibold text-status-bear"
    >
      This page is running an old build ({state.running}) — the numbers may be stale. Tap to reload.
    </button>
  );
}
