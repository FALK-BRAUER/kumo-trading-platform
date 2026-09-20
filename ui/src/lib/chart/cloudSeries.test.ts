import { describe, expect, it } from "vitest";
import type { Time } from "lightweight-charts";
import { buildCloudData, type CloudData } from "./cloudSeries";

const t = (n: number) => n as Time;

describe("buildCloudData", () => {
  it("zips spans aligned by time into a/b cloud points", () => {
    const a = [{ time: t(1), value: 10 }, { time: t(2), value: 12 }];
    const b = [{ time: t(1), value: 8 }, { time: t(2), value: 13 }];
    expect(buildCloudData(a, b)).toEqual([
      { time: t(1), a: 10, b: 8 },
      { time: t(2), a: 12, b: 13 },
    ]);
  });

  it("emits whitespace (no a/b) for a time present only in Span A", () => {
    const a = [{ time: t(1), value: 10 }, { time: t(2), value: 12 }];
    const b = [{ time: t(2), value: 11 }]; // Span B starts later (longer lookback)
    const out = buildCloudData(a, b);
    expect(out[0]).toEqual({ time: t(1) }); // whitespace — no fill before Span B exists
    expect(out[1]).toEqual({ time: t(2), a: 12, b: 11 });
  });

  it("drives fill both ways — a>=b (bull) and b>a (bear) both representable", () => {
    const out = buildCloudData([{ time: t(1), value: 5 }], [{ time: t(1), value: 9 }]) as CloudData[];
    expect(out[0].a).toBeLessThan(out[0].b); // bear point (B above A) — renderer colours it red
  });
});
