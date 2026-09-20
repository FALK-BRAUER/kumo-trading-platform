import { describe, it, expect } from "vitest";
import { feedState } from "./feedState";

describe("feedState", () => {
  it("treats a healthy frame as healthy", () => {
    const s = feedState({ status: "ok", error: null }, true);
    expect(s).toEqual({ healthy: true, blockingMessage: null, banner: null });
  });

  it("says RECONCILING rather than showing an empty book during startup", () => {
    // The legitimate empty window. `held: 0` here is lag, not liquidation — and the operator should not
    // have to know that rule to read their own screen.
    const s = feedState({ status: "seeding" }, false);
    expect(s.healthy).toBe(false);
    expect(s.blockingMessage).toMatch(/reconciling/i);
  });

  it("keeps real rows on failure, banners them, and never calls them current", () => {
    // 2026-08-14: the projection died and the tile blanked. Showing the last known eight positions
    // marked stale is strictly better than showing none.
    const s = feedState({ status: "failed", error: "NameError: name 'cache' is not defined" }, true);
    expect(s.blockingMessage).toBeNull();
    expect(s.banner).toMatch(/NOT UPDATING/);
    expect(s.banner).toMatch(/NameError/);
    expect(s.healthy).toBe(false);
  });

  it("blocks with the reason when it failed and there is nothing to fall back on", () => {
    // The actual 08-14 shape: the FIRST projection after a restart failed, so there was never a good
    // frame to fall back to. This is the case that rendered as a plain empty book.
    const s = feedState({ status: "failed", error: "NameError: cache" }, false);
    expect(s.blockingMessage).toMatch(/unavailable/i);
    expect(s.blockingMessage).toMatch(/NameError/);
  });

  it("still explains itself when the engine reports a failure with no detail", () => {
    const s = feedState({ status: "failed" }, false);
    expect(s.blockingMessage).toMatch(/unavailable/i);
    expect(s.blockingMessage).not.toMatch(/undefined|null/);
  });

  it("treats a frame with NO status as healthy, so an older engine still renders", () => {
    // Refusing to render against an engine that predates this field would be a worse failure than the
    // one it fixes.
    expect(feedState({}, true).healthy).toBe(true);
    expect(feedState(undefined, true).healthy).toBe(true);
  });

  it("does NOT confuse a genuinely flat book with a broken one", () => {
    // The whole point. Reconciled, healthy, and nothing held is a legitimate state that must keep
    // rendering the tile's own "Nothing held" empty label rather than an error.
    const s = feedState({ status: "ok" }, false);
    expect(s).toEqual({ healthy: true, blockingMessage: null, banner: null });
  });
});
