"use client";

/**
 * QueryProvider — singleton TanStack Query client scoped at the board root. Query owns ONLY REST
 * snapshots + (later) layout persistence; the live WS plane stays out of it (#7). Per-source cache
 * windows come from each DataSource (`cacheMs`/`gcMs`), so no global staleTime is set here.
 */
import { useState, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        refetchOnWindowFocus: false,
        refetchOnReconnect: true,
        retry: 2,
      },
    },
  });
}

export function QueryProvider({ children }: { children: ReactNode }) {
  // One client per mount (survives re-renders); the board is a single long-lived client tree.
  const [client] = useState(makeClient);
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
