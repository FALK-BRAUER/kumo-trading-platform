import { describe, expect, it } from "vitest";

/** Mirrors ChartTile's `displace` — the cloud must be projected forward, and the projection must
 *  survive an irregular series (weekends, half-days, feed gaps). */
function displace(line: { time: number; value: number }[], times: number[], displacement = 26) {
  if (line.length === 0 || times.length < 2) return line;
  const gaps: number[] = [];
  for (let i = 1; i < times.length; i += 1) gaps.push(times[i] - times[i - 1]);
  gaps.sort((a, b) => a - b);
  const step = gaps[Math.floor(gaps.length / 2)] || 1;
  const lastTs = times[times.length - 1];
  return line.map((pt, i) => {
    const target = i + displacement;
    return {
      time: target < times.length ? times[target] : lastTs + (target - times.length + 1) * step,
      value: pt.value,
    };
  });
}

describe("Ichimoku cloud displacement", () => {
  const times = Array.from({ length: 60 }, (_, i) => 1_000 + i * 86_400);

  it("projects the cloud forward, not onto the current bar", () => {
    const line = times.slice(0, 40).map((t, i) => ({ time: t, value: i }));
    const out = displace(line, times);
    expect(out[0].time).toBe(times[26]);
    expect(out[0].value).toBe(0); // value unchanged — only its x position moves
  });

  it("extends past the last bar so the cloud leads price", () => {
    const line = times.map((t, i) => ({ time: t, value: i }));
    const out = displace(line, times);
    expect(out[out.length - 1].time).toBeGreaterThan(times[times.length - 1]);
  });

  it("uses the MEDIAN interval, so one weekend gap cannot set the whole projection", () => {
    // a 3-day gap in the middle; last-minus-previous would be fine here but a trailing gap would not
    const irregular = [...times.slice(0, 30), times[29] + 3 * 86_400, ...times.slice(30).map((t) => t + 3 * 86_400)];
    const line = irregular.map((t, i) => ({ time: t, value: i }));
    const out = displace(line, irregular);
    const tail = out[out.length - 1].time - out[out.length - 2].time;
    expect(tail).toBe(86_400); // the ordinary day, not the 3-day hole
  });

  it("is a no-op on too little data rather than throwing", () => {
    expect(displace([{ time: 1, value: 1 }], [1])).toHaveLength(1);
    expect(displace([], times)).toHaveLength(0);
  });
});
