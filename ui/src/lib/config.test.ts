/**
 * The cockpit is reachable over LAN http, tailscale http, AND tailscale https (a real cert, added
 * 2026-08-22 so the phone works without toggling the VPN). The scheme of the API and WS endpoints has
 * to follow the page, and this is the file that got it wrong.
 *
 * THE FAILURE THIS PINS. `new WebSocket("ws://host")` from an `https://` page throws `SecurityError`
 * SYNCHRONOUSLY — not a failed connection, a thrown constructor. It unmounts React and Next renders
 * "Application error: a client-side exception has occurred", with no network request to look at. The
 * whole cockpit is a blank error page; nothing degrades gracefully.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

/** Reload config.ts under a given page location + env. It reads both at module scope. */
async function load(href: string, env: Record<string, string | undefined> = {}) {
  vi.resetModules();
  vi.stubGlobal("window", { location: new URL(href) });
  for (const [k, v] of Object.entries(env)) vi.stubEnv(k, v as string);
  return await import("./config");
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("the fixture reflects how the browser actually behaves", () => {
  it("a URL exposes protocol WITH the trailing colon", () => {
    // The fixture's own property first. `location.protocol` is "https:", not "https" — an
    // implementation comparing against "https" would pass a hand-made double and fail in a browser.
    expect(new URL("https://h:3000/").protocol).toBe("https:");
  });
});

describe("scheme follows the page", () => {
  it("an https page gets https + wss", async () => {
    const c = await load("https://cockpit-host.tailnet0.ts.net:3000/");
    expect(c.API_BASE).toBe("https://cockpit-host.tailnet0.ts.net:8000");
    expect(c.WS_BASE).toBe("wss://cockpit-host.tailnet0.ts.net:8000");
  });

  it("a plain http page still gets http + ws", async () => {
    // The discriminating half. Without it, hardcoding https/wss passes the test above and breaks
    // every LAN and localhost user instead — the same bug pointed the other way.
    const c = await load("http://192.0.2.10:3000/");
    expect(c.API_BASE).toBe("http://192.0.2.10:8000");
    expect(c.WS_BASE).toBe("ws://192.0.2.10:8000");
  });

  it("localhost over http is unaffected", async () => {
    const c = await load("http://localhost:3000/");
    expect(c.API_BASE).toBe("http://localhost:8000");
    expect(c.WS_BASE).toBe("ws://localhost:8000");
  });

  it("NEVER emits a ws:// endpoint to an https page", async () => {
    // Aimed at the CLASS, not the instance: any future rework that reintroduces a literal scheme
    // must trip here, whatever shape it takes.
    const c = await load("https://host.example.ts.net:3000/");
    expect(c.WS_BASE.startsWith("ws://")).toBe(false);
    expect(c.API_BASE.startsWith("http://")).toBe(false);
  });
});

describe("server-side rendering, where there is no page to follow", () => {
  it("falls back to http/localhost rather than guessing https", async () => {
    // Reachable: Next evaluates this module on the server during prerender. The value is replaced on
    // hydration, so the branch is nearly invisible — which is exactly why it was the one mutation the
    // suite did not catch. localhost is never served over TLS here, and an https guess would bake a
    // wrong absolute URL into prerendered HTML.
    vi.resetModules();
    vi.unstubAllGlobals();
    vi.stubGlobal("window", undefined);
    const c = await import("./config");
    expect(c.API_BASE).toBe("http://localhost:8000");
    expect(c.WS_BASE).toBe("ws://localhost:8000");
  });
});

describe("an explicit override still wins", () => {
  it("NEXT_PUBLIC_* is used verbatim, scheme included", async () => {
    const c = await load("https://page.example:3000/", {
      NEXT_PUBLIC_API_BASE: "https://api.example",
      NEXT_PUBLIC_WS_BASE: "wss://api.example",
    });
    expect(c.API_BASE).toBe("https://api.example");
    expect(c.WS_BASE).toBe("wss://api.example");
  });

  it('empty string is still treated as unset, not as a relative URL', async () => {
    // The 2026-07 regression: the Docker build bakes NEXT_PUBLIC_* to "" when the arg is omitted, and
    // `??` kept it — yielding a relative URL that throws in `new URL()`. `||` is load-bearing.
    const c = await load("https://page.example:3000/", {
      NEXT_PUBLIC_API_BASE: "",
      NEXT_PUBLIC_WS_BASE: "",
    });
    expect(c.API_BASE).toBe("https://page.example:8000");
    expect(c.WS_BASE).toBe("wss://page.example:8000");
  });
});

describe("the API port belongs to the INSTANCE, not to the platform", () => {
  /**
   * Two instances run on one host: kumo-paper's API on 8000, staging-ibkr's on 8010. The port was a
   * literal `8000` in this file, baked into every UI image at build time — so staging's UI asked
   * PAPER's API for its data (2026-08-24). Over plain http that is a silent cross-instance read;
   * over tailscale https it failed loudly as "Cockpit API unreachable", which is the only reason it
   * was noticed at all.
   *
   * The host still comes from the browser, so LAN and mobile keep working without a rebuild. Only
   * the port is per-instance.
   */
  it("defaults to 8000 when the instance does not say otherwise", async () => {
    // The fixture's own property first: if the default moved, every assertion below would still pass
    // while every existing deployment broke.
    const c = await load("http://localhost:3000/", { NEXT_PUBLIC_API_PORT: undefined });
    expect(c.API_BASE).toBe("http://localhost:8000");
  });

  it("uses the instance's port for both REST and WS", async () => {
    const c = await load("https://cockpit-host.tailnet0.ts.net:3010/", {
      NEXT_PUBLIC_API_PORT: "8010",
    });
    expect(c.API_BASE).toBe("https://cockpit-host.tailnet0.ts.net:8010");
    expect(c.WS_BASE).toBe("wss://cockpit-host.tailnet0.ts.net:8010");
  });

  it("two instances on one host never resolve to the same API", async () => {
    // The discriminating half, aimed at the CLASS: a rework that reads the port but drops it on one
    // of the two bases passes the test above and still crosses the instances.
    const paper = await load("http://host:3000/", { NEXT_PUBLIC_API_PORT: "8000" });
    const staging = await load("http://host:3010/", { NEXT_PUBLIC_API_PORT: "8010" });
    expect(staging.API_BASE).not.toBe(paper.API_BASE);
    expect(staging.WS_BASE).not.toBe(paper.WS_BASE);
  });

  it("an empty string is treated as unset, like the other NEXT_PUBLIC_ vars", async () => {
    // Docker bakes ARG-with-no-value to "", and `Number("")` is 0 — which would produce `host:0`.
    const c = await load("http://localhost:3000/", { NEXT_PUBLIC_API_PORT: "" });
    expect(c.API_BASE).toBe("http://localhost:8000");
  });
});
