import { describe, it, expect } from "vitest";
import { etMinutes, minutesFrom, sessionOf, sessionRuns } from "./sessionRuns";

/** Epoch SECONDS for an ET wall-clock time on a given August day (EDT, UTC-4). */
const et = (h: number, m = 0, day = 25) =>
  Date.parse(`2026-08-${String(day).padStart(2, "0")}T${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:00-04:00`) / 1000;

describe("etMinutes", () => {
  it("reads the ET wall clock, not UTC and not local", () => {
    expect(etMinutes(et(9, 30))).toBe(9 * 60 + 30);
    expect(etMinutes(et(16, 0))).toBe(16 * 60);
  });

  it("normalises an hour of 24, which this runtime never produces", () => {
    // THIS TEST USED TO FORMAT A REAL MIDNIGHT AND ASSERT 5, AND IT WAS VACUOUS: `hour12: false`
    // returns "00" here, so deleting the `% 24` guard left it green — measured by doing exactly that.
    // Some engines return "24" instead, which would put 00:05 at 1445 minutes: after the close rather
    // than before the pre-open, i.e. classified `closed` either way but by luck, and wrong the moment
    // the boundaries move.
    //
    // Only falsifiable if the hour can be supplied directly, so it is.
    expect(minutesFrom(24, 5)).toBe(5);
    expect(minutesFrom(0, 5)).toBe(5);
    expect(minutesFrom(9, 30)).toBe(570);
    // And the real path still agrees for this runtime.
    expect(etMinutes(et(0, 5))).toBe(5);
  });

  it("follows US daylight saving instead of a fixed offset", () => {
    // Same wall-clock hour, opposite sides of the DST boundary. A hardcoded UTC offset would be
    // right for one of these and silently wrong for the other.
    const summer = Date.parse("2026-08-25T09:30:00-04:00") / 1000; // EDT
    const winter = Date.parse("2026-01-15T09:30:00-05:00") / 1000; // EST
    expect(etMinutes(summer)).toBe(570);
    expect(etMinutes(winter)).toBe(570);
  });
});

describe("sessionOf", () => {
  it("classifies the boundaries the way the exchange does", () => {
    expect(sessionOf(et(3, 59))).toBe("closed");
    expect(sessionOf(et(4, 0))).toBe("pre"); // inclusive
    expect(sessionOf(et(9, 29))).toBe("pre");
    expect(sessionOf(et(9, 30))).toBe("regular"); // inclusive
    expect(sessionOf(et(15, 59))).toBe("regular");
    expect(sessionOf(et(16, 0))).toBe("post"); // exclusive of regular
    expect(sessionOf(et(19, 59))).toBe("post");
    expect(sessionOf(et(20, 0))).toBe("closed");
  });
});

describe("sessionRuns", () => {
  const pts = (...times: number[]) => times.map((t) => ({ t, equity: 100 }));

  it("THE FIXTURE ITSELF SPANS THE OPEN, or nothing below discriminates", () => {
    // A fixture entirely inside one session yields one run whatever the code does — it would pass
    // with the splitting removed. Assert the input actually crosses a boundary first.
    const p = pts(et(9, 0), et(9, 29), et(9, 30), et(10, 0));
    expect(new Set(p.map((x) => sessionOf(x.t))).size).toBeGreaterThan(1);
  });

  it("splits pre-market from the regular session", () => {
    const runs = sessionRuns(pts(et(8, 0), et(9, 0), et(10, 0), et(11, 0)));
    expect(runs.map((r) => r.session)).toEqual(["pre", "regular"]);
  });

  it("splits the regular session from post-market", () => {
    const runs = sessionRuns(pts(et(15, 0), et(15, 59), et(16, 30), et(17, 0)));
    expect(runs.map((r) => r.session)).toEqual(["regular", "post"]);
  });

  it("REPEATS THE BOUNDARY POINT so the drawn line has no gap", () => {
    // Without the overlap the open renders as a BREAK in the curve — introducing exactly the
    // artefact this feature exists to explain.
    const a = et(9, 29);
    const b = et(9, 30);
    const runs = sessionRuns(pts(et(9, 0), a, b, et(10, 0)));

    expect(runs[0].points[runs[0].points.length - 1].t).toBe(a);
    expect(runs[1].points[0].t).toBe(a); // the pre-market point, repeated
    expect(runs[1].points[1].t).toBe(b);
  });

  it("returns a single run when the series never leaves one session", () => {
    const runs = sessionRuns(pts(et(10, 0), et(11, 0), et(12, 0)));
    expect(runs).toHaveLength(1);
    expect(runs[0].session).toBe("regular");
    expect(runs[0].points).toHaveLength(3); // no phantom repeated point on the first run
  });

  it("handles an empty series without inventing a run", () => {
    expect(sessionRuns([])).toEqual([]);
  });

  it("covers a full extended day in order", () => {
    const runs = sessionRuns(pts(et(5, 0), et(8, 0), et(10, 0), et(14, 0), et(17, 0), et(19, 0)));
    expect(runs.map((r) => r.session)).toEqual(["pre", "regular", "post"]);
  });
});
