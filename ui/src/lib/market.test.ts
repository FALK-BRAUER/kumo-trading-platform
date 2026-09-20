import { describe, it, expect } from "vitest";
import { isUsMarketOpen, marketOrderWouldBeCanceled } from "./market";

// Summer = EDT (UTC-4): 14:00 UTC = 10:00 ET open; 02:00 UTC = 22:00 ET prev day → closed.
const WEEKDAY_OPEN = Date.UTC(2026, 6, 14, 14, 0); // Tue 10:00 ET
const WEEKDAY_NIGHT = Date.UTC(2026, 6, 14, 2, 0); // Tue 22:00 ET Mon → closed
const SATURDAY = Date.UTC(2026, 6, 11, 14, 0); // Sat → closed
// Winter = EST (UTC-5) — these would FAIL a hardcoded -4 offset: 20:30 UTC = 15:30 ET (open, not 16:30);
// 13:30 UTC = 08:30 ET (pre-market, closed, not 09:30 open).
const WINTER_AFTERNOON = Date.UTC(2026, 0, 6, 20, 30); // Tue 15:30 EST → open
const WINTER_PREMARKET = Date.UTC(2026, 0, 6, 13, 30); // Tue 08:30 EST → closed

describe("isUsMarketOpen", () => {
  it("open during regular weekday session, closed at night and on weekends", () => {
    expect(isUsMarketOpen(WEEKDAY_OPEN)).toBe(true);
    expect(isUsMarketOpen(WEEKDAY_NIGHT)).toBe(false);
    expect(isUsMarketOpen(SATURDAY)).toBe(false);
  });
  it("handles EST (winter) correctly — not a fixed offset", () => {
    expect(isUsMarketOpen(WINTER_AFTERNOON)).toBe(true);
    expect(isUsMarketOpen(WINTER_PREMARKET)).toBe(false);
  });
});

describe("marketOrderWouldBeCanceled", () => {
  it("blocks a MARKET order only when the market is closed", () => {
    expect(marketOrderWouldBeCanceled("market", WEEKDAY_NIGHT)).toBe(true);
    expect(marketOrderWouldBeCanceled("market", WEEKDAY_OPEN)).toBe(false);
  });
  it("never blocks limit/stop (they rest until open)", () => {
    expect(marketOrderWouldBeCanceled("limit", WEEKDAY_NIGHT)).toBe(false);
    expect(marketOrderWouldBeCanceled("stop", WEEKDAY_NIGHT)).toBe(false);
  });
});
