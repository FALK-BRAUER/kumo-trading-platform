import { describe, it, expect } from "vitest";
import { toneForStatus } from "./status";

describe("StatusBadge tone", () => {
  it("DENIED and REJECTED are prominent errors", () => {
    expect(toneForStatus("DENIED")).toBe("error");
    expect(toneForStatus("REJECTED")).toBe("error");
    expect(toneForStatus("denied")).toBe("error");
  });
  it("FILLED quiet-filled; PARTIALLY_FILLED partial", () => {
    expect(toneForStatus("FILLED")).toBe("filled");
    expect(toneForStatus("PARTIALLY_FILLED")).toBe("partial");
  });
  it("CANCELED / EXPIRED are terminal-but-not-error", () => {
    expect(toneForStatus("CANCELED")).toBe("done");
    expect(toneForStatus("EXPIRED")).toBe("done");
  });
  it("live states fall through to working", () => {
    expect(toneForStatus("ACCEPTED")).toBe("working");
    expect(toneForStatus("PENDING_NEW")).toBe("working");
  });
});
