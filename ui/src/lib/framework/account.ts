"use client";

/**
 * useAccount (#41) — the account plane (equity / cash / buying_power / multiplier). A singleton (not
 * per-symbol), so it binds the shared `account` DataSource directly rather than going through
 * useInstrument. Values are Alpaca's own (portfolio_value → equity, real margin buying_power); the order
 * ticket sizes on `equity` and gates affordability on `buying_power`.
 */
import { useSource } from "./datasource/useSource";
import type { SourceStatus } from "./types";
import type { AccountDTO } from "@/lib/api/types";

export function useAccount(): { account: AccountDTO | null; status: SourceStatus } {
  const { data, status } = useSource("account", {});
  const account = (data as { account?: AccountDTO | null } | undefined)?.account ?? null;
  return { account, status };
}
