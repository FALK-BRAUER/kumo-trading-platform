# ui/

The Next.js cockpit. Render-only: every number on screen comes from the backend over REST or WebSocket.

- **Goes in:** `src/app/` routes, `src/components/`, `src/lib/` client + state; tests next to the source as `*.test.tsx`
- **Stays out:** trading logic, computed values that the backend should own
