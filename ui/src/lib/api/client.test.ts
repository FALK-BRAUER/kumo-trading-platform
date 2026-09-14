import { describe, expect, it } from "vitest";
import { apiBase } from "./client";

describe("apiBase", () => {
  it("defaults to localhost when the env var is unset", () => {
    expect(apiBase(undefined)).toBe("http://127.0.0.1:8000");
  });
});
