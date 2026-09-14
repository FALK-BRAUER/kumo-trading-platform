# datasource — the data plane

How tiles get data without fetching. A tile declares source *names* (`dataSources: string[]`); the
container resolves them here and passes `data`/`status` down.

- `registry.ts` — `name → DataSource` (`kind: rest|ws|hybrid`, endpoint/topic, params, cacheMs/gcMs).
- `useSource` / `useSources` (`useSource.ts`) — resolve names to `{ data, status }`. Switches on `kind`
  internally so the WS swap is invisible to tiles. REST path wired; ws/hybrid stubbed until phase 3.
- `QueryProvider.tsx` — singleton TanStack Query client (REST snapshots + layout persistence only).
- `ws-manager.ts` *(phase 3)* — one multiplexed socket, ref-counted topic subs, reconnect/resubscribe.

The actual source→endpoint/topic wiring lives in `@/config/datasources`, not here. This dir is the
generic machinery; topic naming stays explicit and boring (`positions`, `bars:{symbol}`).
