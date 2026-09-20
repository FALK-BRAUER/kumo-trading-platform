/**
 * Runtime endpoints for the FastAPI/Nautilus bridge.
 *
 * By default the bridge is assumed to live on the SAME host that served the UI, port 8000 — so
 * opening the cockpit over LAN IP, tailscale, or localhost all resolve the API to the right machine
 * without rebuilds (key for mobile). Override explicitly with NEXT_PUBLIC_API_BASE / _WS_BASE.
 * Public bridge URLs only — never secrets.
 *
 * THE SCHEME FOLLOWS THE PAGE, and that is not cosmetic. These were hardcoded `http://` and `ws://`,
 * which worked for every origin the cockpit had until it was published over tailscale HTTPS with a
 * real cert. From an `https://` page, `new WebSocket("ws://…")` throws SecurityError SYNCHRONOUSLY —
 * a thrown constructor, not a failed connection — so React unmounted and Next rendered "Application
 * error: a client-side exception has occurred" with no network request to inspect. Mixed-content
 * blocking killed the REST calls in the same stroke. The whole cockpit was a blank error page.
 */
function loc(): { hostname: string; protocol: string } {
  if (typeof window !== "undefined") return window.location;
  // SSR: no page to follow. http/localhost is the safe assumption — this value is replaced on hydration.
  return { hostname: "localhost", protocol: "http:" };
}

//: `location.protocol` carries the trailing colon ("https:"), which is why the comparison includes it.
function schemes(): { http: string; ws: string } {
  return loc().protocol === "https:" ? { http: "https", ws: "wss" } : { http: "http", ws: "ws" };
}

// Treat unset AND empty-string as "not provided": the Docker build bakes NEXT_PUBLIC_* to "" when the
// build arg is omitted (ARG with no value → ENV=""), and `??` would keep that "" — yielding a relative
// URL that throws in `new URL(...)` / `new WebSocket(...)`. `||` falls back to the browser-host default.
const envApi = process.env.NEXT_PUBLIC_API_BASE;
const envWs = process.env.NEXT_PUBLIC_WS_BASE;

// THE PORT BELONGS TO THE INSTANCE. It was a literal 8000, baked into every UI image at build time,
// so a second instance's UI asked the FIRST instance's API for its data: ibkr-paper-retired's UI (its own
// API on 8010) read kumo-paper's on 8000. Over plain http that is a silent cross-instance read; over
// tailscale https it surfaced as "Cockpit API unreachable", which is the only reason it was caught.
// Empty-string is treated as unset for the same reason as the two above — Docker bakes an ARG with no
// value to "", and `host:` with nothing after it is not a URL anyone can debug.
const envPort = process.env.NEXT_PUBLIC_API_PORT;
const apiPort = envPort && envPort.length > 0 ? envPort : "8000";

export const API_BASE =
  envApi && envApi.length > 0 ? envApi : `${schemes().http}://${loc().hostname}:${apiPort}`;
export const WS_BASE =
  envWs && envWs.length > 0 ? envWs : `${schemes().ws}://${loc().hostname}:${apiPort}`;
