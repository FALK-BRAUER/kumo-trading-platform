import { describe, it, expect } from "vitest";
import { readAge } from "./readAge";

const NOW = Date.parse("2026-08-20T10:41:00Z"); // 06:41 ET, the moment the operator asked

describe("the rotation read states its own age (#351 follow-up)", () => {
  it("reports the real payload as most of a day old", () => {
    // The exact payload on screen when the operator asked "premarket or yesterday?" — generated mid-session the
    // PREVIOUS day. The answer was neither, and nothing on the tile said so.
    const a = readAge("2026-08-19T18:05:48+00:00", NOW)!;
    expect(Math.round(a.hours)).toBe(17);
    expect(a.tone).toBe("stale");
    expect(a.label).toBe("17h old");
  });

  it("calls it STALE because a session closed since, not because N hours passed", () => {
    // THE 16.7h READ IS INSIDE ANY 24-HOUR THRESHOLD and is still stale: it was generated 14:05 ET, two
    // hours BEFORE that day's close, so it never saw the session it claims to describe. An hour count
    // cannot express that — this was my first rule and it graded the real payload "aging".
    expect(readAge("2026-08-19T18:05:48+00:00", NOW)!.tone).toBe("stale");   // pre-close yesterday
    expect(readAge("2026-08-19T21:30:00+00:00", NOW)!.tone).toBe("aging");   // 17:30 ET, AFTER the close
    expect(readAge("2026-08-20T09:41:00Z", NOW)!.tone).toBe("fresh");        // an hour ago
  });

  it("never renders a future payload as healthy", () => {
    // A clock skew between the generating host and the reader would otherwise show a negative age as
    // "fresh" — the most reassuring possible rendering of a broken input.
    const a = readAge("2026-08-21T10:41:00Z", NOW)!;
    expect(a.tone).toBe("stale");
    expect(a.label).toMatch(/future/);
  });

  it("says nothing rather than guessing when there is no timestamp", () => {
    expect(readAge(null, NOW)).toBeNull();
    expect(readAge("not a date", NOW)).toBeNull();
  });

  it("reads in units a person can act on", () => {
    expect(readAge("2026-08-20T10:11:00Z", NOW)!.label).toBe("30m old");
    expect(readAge("2026-08-17T10:41:00Z", NOW)!.label).toBe("3d old");
  });
});
