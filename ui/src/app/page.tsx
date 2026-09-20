import { Board } from "@/components/board/Board";
import { BusyBanner } from "@/components/board/BusyBanner";
import { FeedBadge } from "@/components/board/FeedBadge";
import { HealthBanner } from "@/components/board/HealthBanner";
import { GlobalSymbolSearch } from "@/components/search/GlobalSymbolSearch";
import { StaleBundleBanner } from "@/components/board/StaleBundleBanner";
import { QueryProvider } from "@/lib/framework/datasource/QueryProvider";
import { instanceBadgeLabel } from "@/lib/instanceBadge";

// Environment badge — driven by NEXT_PUBLIC_ENV (paper | live), baked per stack at build (#21).
// LIVE is red and unmissable; anything not "live" renders as PAPER (safe default).
const IS_LIVE = process.env.NEXT_PUBLIC_ENV === "live";
// Which TENANT (#610) — PAPER/LIVE says account kind, not which stack; two paper tenants rendered
// identically and a staging screenshot got debugged as the alpaca instance. Baked per stack.
const INSTANCE_LABEL = instanceBadgeLabel(process.env.NEXT_PUBLIC_INSTANCE);

export default function Home() {
  return (
    <main className="min-h-screen">
      {/* QueryProvider wraps the header too — the global symbol search (#25) lives there and uses useQuery. */}
      <QueryProvider>
        {/* The env badge must NEVER be obscured — it is the one thing telling you whether an order goes
            to a paper account or a real one. On a 390px phone the row needs 427px (title 101 + search
            224 + badge 46 + padding and gaps), so something has to give: the SEARCH shrinks, the badge
            does not. `shrink-0` on the badge and a fluid search below `sm` — previously the search was
            fixed at w-48 and the badge merely `whitespace-nowrap`, so they overlapped. */}
        {/* ABOVE the header, full width, and only when the running bundle is genuinely behind the one
            the server is serving (#345 item 5). A phone rendered a 10-minute-old bundle showing
            NET·all +$847.61 against a deployed +$250.64 — wrong money, indistinguishable from live.
            Placed here rather than in a tile because it invalidates EVERY number on the page, not one
            panel's. */}
        <StaleBundleBanner />
        <header className="flex items-center justify-between gap-2 border-b border-ds-line px-4 py-2.5">
          <div className="flex min-w-0 flex-1 items-center gap-3">
            <h1 className="hidden whitespace-nowrap font-mono text-sm font-bold text-t1 sm:block">kumo-trading-platform</h1>
            <GlobalSymbolSearch />
          </div>
          {/* FEED first, env badge last — the env badge is the one that must never be obscured, so it
              keeps the outer edge. FEED is deliberately NOT called "LIVE": that word is taken two
              elements right and means real-money orders (#384). */}
          <span className="shrink-0 whitespace-nowrap rounded bg-t2/10 px-2 py-0.5 font-mono text-[10px] font-bold text-t2 ring-1 ring-t2/30">
            {INSTANCE_LABEL}
          </span>
          <FeedBadge />
          {IS_LIVE ? (
            <span className="shrink-0 whitespace-nowrap rounded bg-status-bear/15 px-2 py-0.5 font-mono text-[10px] font-bold text-status-bear ring-1 ring-status-bear/40">
              ● LIVE
            </span>
          ) : (
            <span className="shrink-0 whitespace-nowrap rounded bg-status-watch/40 px-2 py-0.5 font-mono text-[10px] font-bold text-status-watch ring-1 ring-status-watch/50">
              PAPER
            </span>
          )}
        </header>
        <HealthBanner />
        <BusyBanner />
        <Board />
      </QueryProvider>
    </main>
  );
}
