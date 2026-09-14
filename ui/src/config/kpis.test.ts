import { describe, it, expect, beforeAll } from "vitest";
import { getKpi } from "@/lib/framework/kpi/registry";
import type { KpiContext } from "@/lib/framework/kpi/types";
import "./kpis"; // side-effect: registers "volume"/"bid_ask_size"

function ctx(overrides?: Partial<KpiContext>): KpiContext {
  return {
    instrumentId: "TEST.XNAS", price: null, quote: null, bars: [], vwap: null, todayRange: null,
    fundamentals: null, ...overrides,
  };
}

describe("volume KPI", () => {
  it("formats the last bar's volume, compacted", () => {
    const bars = [{ instrument_id: "TEST.XNAS", ts_event: 0, open: 1, high: 1, low: 1, close: 1, volume: 1_234_000 }];
    expect(getKpi("volume")!.format(ctx({ bars }))).toBe("1.2M");
  });
  it("no bars → null, not a placeholder", () => {
    expect(getKpi("volume")!.format(ctx())).toBeNull();
  });
  it("sortValue is the raw number, not the formatted string", () => {
    const bars = [{ instrument_id: "TEST.XNAS", ts_event: 0, open: 1, high: 1, low: 1, close: 1, volume: 1_234_000 }];
    expect(getKpi("volume")!.sortValue!(ctx({ bars }))).toBe(1_234_000);
    expect(getKpi("volume")!.sortValue!(ctx())).toBeNull();
  });
});

describe("bid_ask_size KPI", () => {
  it("formats bid x ask size, compacted", () => {
    const quote = { bid: 1, ask: 1.01, mid: 1.005, spread: 0.01, bidSize: 500, askSize: 12000 };
    expect(getKpi("bid_ask_size")!.format(ctx({ quote }))).toBe("500 x 12.0K");
  });
  it("no quote yet → null", () => {
    expect(getKpi("bid_ask_size")!.format(ctx())).toBeNull();
  });
});

describe("vwap KPI", () => {
  it("formats to 2 decimals", () => {
    expect(getKpi("vwap")!.format(ctx({ vwap: 123.456 }))).toBe("123.46");
  });
  it("no session volume yet → null, not a stale/zero value", () => {
    expect(getKpi("vwap")!.format(ctx())).toBeNull();
  });
  it("sortValue is the raw number", () => {
    expect(getKpi("vwap")!.sortValue!(ctx({ vwap: 123.456 }))).toBe(123.456);
    expect(getKpi("vwap")!.sortValue!(ctx())).toBeNull();
  });
});

describe("today_range KPI", () => {
  it("formats as low-high", () => {
    const todayRange = { high: 172.34, low: 169.8, prevClose: 169.75 };
    expect(getKpi("today_range")!.format(ctx({ todayRange }))).toBe("169.80-172.34");
  });
  it("no snapshot yet → null", () => {
    expect(getKpi("today_range")!.format(ctx())).toBeNull();
  });
});

describe("prior_close KPI", () => {
  it("formats to 2 decimals", () => {
    const todayRange = { high: 172.34, low: 169.8, prevClose: 169.75 };
    expect(getKpi("prior_close")!.format(ctx({ todayRange }))).toBe("169.75");
  });
  it("no snapshot yet → null", () => {
    expect(getKpi("prior_close")!.format(ctx())).toBeNull();
  });
  it("sortValue is the raw number", () => {
    const todayRange = { high: 172.34, low: 169.8, prevClose: 169.75 };
    expect(getKpi("prior_close")!.sortValue!(ctx({ todayRange }))).toBe(169.75);
    expect(getKpi("prior_close")!.sortValue!(ctx())).toBeNull();
  });
});

const FUND = { marketCap: 4_537_000_000_000, beta: 1.1, eps: 8.23, pe: 37.5, dividendAmount: 1.05 };

describe("market_cap KPI", () => {
  it("formats compact", () => {
    expect(getKpi("market_cap")!.format(ctx({ fundamentals: FUND }))).toBe("4.5T");
  });
  it("no fundamentals yet → null", () => {
    expect(getKpi("market_cap")!.format(ctx())).toBeNull();
  });
  it("sortValue is the raw number", () => {
    expect(getKpi("market_cap")!.sortValue!(ctx({ fundamentals: FUND }))).toBe(4_537_000_000_000);
  });
  it("formats billions and millions with the right tier, not a huge M/K string", () => {
    // The operator feedback: "4537071.0M" was 12 chars and single-handedly blew the KPI row past one line —
    // T/B tiers keep large caps short.
    expect(getKpi("market_cap")!.format(ctx({ fundamentals: { ...FUND, marketCap: 133_245_400_000 } }))).toBe("133.2B");
    expect(getKpi("market_cap")!.format(ctx({ fundamentals: { ...FUND, marketCap: 2_696_900_000 } }))).toBe("2.7B");
    expect(getKpi("market_cap")!.format(ctx({ fundamentals: { ...FUND, marketCap: 500_000 } }))).toBe("500.0K");
  });
});

describe("beta KPI", () => {
  it("formats to 2 decimals", () => {
    expect(getKpi("beta")!.format(ctx({ fundamentals: FUND }))).toBe("1.10");
  });
  it("no fundamentals yet → null", () => {
    expect(getKpi("beta")!.format(ctx())).toBeNull();
  });
});

describe("eps KPI", () => {
  it("formats to 2 decimals", () => {
    expect(getKpi("eps")!.format(ctx({ fundamentals: FUND }))).toBe("8.23");
  });
  it("no fundamentals yet → null", () => {
    expect(getKpi("eps")!.format(ctx())).toBeNull();
  });
});

describe("pe KPI", () => {
  it("formats to 1 decimal", () => {
    expect(getKpi("pe")!.format(ctx({ fundamentals: FUND }))).toBe("37.5");
  });
  it("no fundamentals yet → null", () => {
    expect(getKpi("pe")!.format(ctx())).toBeNull();
  });
  it("sortValue is the raw number", () => {
    expect(getKpi("pe")!.sortValue!(ctx({ fundamentals: FUND }))).toBe(37.5);
  });
});

describe("dividend_amount KPI", () => {
  it("formats to 2 decimals", () => {
    expect(getKpi("dividend_amount")!.format(ctx({ fundamentals: FUND }))).toBe("1.05");
  });
  it("no fundamentals yet → null", () => {
    expect(getKpi("dividend_amount")!.format(ctx())).toBeNull();
  });
});
