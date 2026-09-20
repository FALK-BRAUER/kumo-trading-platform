/**
 * Pool tile — the symbols MOMENTUM ranks each session, and the operator whitelist/blacklist.
 *
 * This is the cockpit's pool surface. It replaces a separate web app that ran beside the strategy on
 * its own port: one UI, one design language, reachable wherever the cockpit is.
 *
 * A blacklisted symbol is LISTED, not filtered away. If excluding a name simply removed its row, the
 * only evidence of the decision would be an absence — and an absence is exactly what you cannot see.
 */
"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { DataRow, DataTable, type DataColumn } from "@/components/ds/DataTable";
import { clearPoolOverride, getPool, setPoolOverride, type PoolEntry, type PoolResponse } from "@/lib/api/client";
import type { TileProps } from "@/lib/framework/types";
import type { PoolConfig } from "./schema";

const COLS: DataColumn[] = [
  { label: "Symbol" },
  { label: "State", align: "left" },
  { label: "Source", align: "left", className: "hidden sm:table-cell" },
  { label: "", align: "right", cellClassName: "px-1 pt-1" },
];

type Filter = "all" | "held" | "whitelist" | "blacklist";

const FILTERS: { key: Filter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "held", label: "Held" },
  { key: "whitelist", label: "Whitelist" },
  { key: "blacklist", label: "Blacklist" },
];

function matches(entry: PoolEntry, filter: Filter): boolean {
  if (filter === "held") return entry.held;
  if (filter === "whitelist") return entry.override === "pin";
  if (filter === "blacklist") return entry.override === "exclude";
  return true;
}

/** Held / pinned / excluded, as a chip. Semantic colour, not the accent. */
function StateChip({ entry }: { entry: PoolEntry }) {
  const [text, cls] =
    entry.override === "exclude"
      ? ["BLACKLIST", "text-status-bear"]
      : entry.override === "pin"
        ? ["WHITELIST", "text-status-bull"]
        : entry.held
          ? ["HELD", "text-t1"]
          : ["—", "text-t3"];
  return <span className={`font-mono text-[10px] tracking-wide ${cls}`}>{text}</span>;
}

export function PoolTile({ config }: TileProps<PoolConfig>) {
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<Filter>(config.filter ?? "all");
  const [pin, setPin] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["pool"],
    queryFn: getPool,
    refetchInterval: 30_000,
  });

  const onWrite = (res: PoolResponse) => queryClient.setQueryData(["pool"], res);
  const add = useMutation({ mutationFn: setPoolOverride, onSuccess: onWrite });
  const clear = useMutation({
    mutationFn: ({ symbol, kind }: { symbol: string; kind: "pin" | "exclude" }) => clearPoolOverride(symbol, kind),
    onSuccess: onWrite,
  });

  if (error) {
    return <div className="p-3 font-mono text-[11px] text-status-bear">Pool unavailable — {String(error)}</div>;
  }

  const entries = data?.symbols ?? [];
  const visible = entries.filter((e) => matches(e, filter));
  const counts = {
    all: entries.length,
    held: entries.filter((e) => e.held).length,
    whitelist: entries.filter((e) => e.override === "pin").length,
    blacklist: entries.filter((e) => e.override === "exclude").length,
  };
  const staleSources = (data?.sources ?? []).filter((s) => s.stale);

  const submitPin = (e: React.FormEvent) => {
    e.preventDefault();
    const symbol = pin.trim().toUpperCase();
    if (symbol) {
      add.mutate({ symbol, kind: "pin" });
      setPin("");
    }
  };

  return (
    <div className="flex h-full flex-col gap-2 p-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="font-mono text-sm font-bold text-t1">POOL</h2>
        <span className="font-mono text-[11px] text-t3">
          {isLoading ? "loading…" : `${data?.count ?? 0} rankable · ${counts.held} held`}
        </span>
      </div>

      {/* A stale source is a HARD BLOCK on deciding, so it is stated here rather than left to a log. */}
      {staleSources.length > 0 && (
        <div className="rounded border border-status-bear/40 px-2 py-1 font-mono text-[10px] text-status-bear">
          {staleSources.map((s) => `${s.name}: ${s.status}`).join(" · ")} — the strategy will refuse to decide
        </div>
      )}

      <div className="flex flex-wrap gap-1">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            type="button"
            onClick={() => setFilter(f.key)}
            aria-pressed={filter === f.key}
            className={`rounded px-2 py-0.5 font-mono text-[10px] uppercase tracking-wide transition-colors ${
              filter === f.key ? "bg-ds-surf2 text-t1" : "text-t3 hover:text-t1"
            }`}
          >
            {f.label} {counts[f.key]}
          </button>
        ))}
      </div>

      <form onSubmit={submitPin} className="flex gap-1">
        <input
          value={pin}
          onChange={(e) => setPin(e.target.value)}
          placeholder="Symbol to whitelist"
          aria-label="Symbol to whitelist"
          className="min-w-0 flex-1 rounded border border-ds-line bg-ds-surf px-2 py-1 font-mono text-[11px] text-t1 placeholder:text-t3"
        />
        <button
          type="submit"
          disabled={!pin.trim() || add.isPending}
          className="rounded border border-ds-line px-2 py-1 font-mono text-[10px] uppercase text-t2 hover:text-t1 disabled:opacity-40"
        >
          Whitelist
        </button>
      </form>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <DataTable columns={COLS}>
          {visible.map((entry) => {
            const excluded = entry.override === "exclude";
            const held = entry.meta?.days_held;
            return (
              <DataRow
                key={entry.symbol}
                columns={COLS}
                className={excluded ? "opacity-60" : undefined}
                cells={[
                  <span key="s" className="font-mono text-[13px] font-bold text-t1">
                    {entry.symbol}
                  </span>,
                  <StateChip key="st" entry={entry} />,
                  <span key="p" className="font-mono text-[10px] text-t3">
                    {entry.provenance}
                  </span>,
                  entry.override ? (
                    <button
                      key="a"
                      type="button"
                      onClick={() => clear.mutate({ symbol: entry.symbol, kind: entry.override as "pin" | "exclude" })}
                      className="rounded px-1.5 py-0.5 font-mono text-[10px] text-t3 hover:bg-ds-surf2 hover:text-t1"
                    >
                      clear
                    </button>
                  ) : (
                    <button
                      key="a"
                      type="button"
                      onClick={() => add.mutate({ symbol: entry.symbol, kind: "exclude" })}
                      className="rounded px-1.5 py-0.5 font-mono text-[10px] text-t3 hover:bg-ds-surf2 hover:text-status-bear"
                    >
                      blacklist
                    </button>
                  ),
                ]}
                sub={
                  <span className="font-mono text-[10px] text-t3">
                    {excluded
                      ? `excluded${entry.reason ? ` · ${entry.reason}` : ""} — will be liquidated at the next session`
                      : [
                          entry.sources.join(" + ") || "no source",
                          typeof held === "number" ? `held ${held}d` : null,
                        ]
                          .filter(Boolean)
                          .join(" · ")}
                  </span>
                }
              />
            );
          })}
        </DataTable>
        {!isLoading && visible.length === 0 && (
          <p className="px-2 py-3 font-mono text-[11px] text-t3">Nothing in this view.</p>
        )}
      </div>
    </div>
  );
}
