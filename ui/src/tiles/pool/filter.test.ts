import { describe, expect, it } from "vitest";
import { poolConfigSchema } from "./schema";

describe("pool config schema", () => {
  it("degrades an unknown filter to 'all' rather than applying an impossible one", () => {
    // A stale localStorage value must not leave the tile showing nothing with no way back.
    const parsed = poolConfigSchema.safeParse({ filter: "pinned-maybe" });
    expect(parsed.success).toBe(false);
    expect(poolConfigSchema.parse({}).filter).toBe("all");
  });

  it("accepts every filter the tile actually renders", () => {
    for (const f of ["all", "held", "whitelist", "blacklist"]) {
      expect(poolConfigSchema.parse({ filter: f }).filter).toBe(f);
    }
  });
});
