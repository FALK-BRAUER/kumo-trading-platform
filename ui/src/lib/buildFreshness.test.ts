/**
 * A stale bundle must not render as a live one (#345 item 5).
 *
 * WHAT HAPPENED. A phone served a 10-minute-old bundle showing NET·all **+$847.61** when the deployed
 * answer was **+$250.64**. Wrong money, indistinguishable from a live read. Same class as #336/#343 — a
 * plausible wrong number in the hero slot that nobody re-checks — except the defect is in DELIVERY, so
 * no correctness in the engine can prevent it. The operator debugs from that phone, which makes it his primary
 * read of the account.
 *
 * THE COMPARISON THAT WOULD HAVE BEEN WRONG. #345 proposed flagging "when the bundle's build id
 * disagrees with the one the API reports". `deploy/Makefile` computes the UI's `BUILD_ID` from
 * `git log -1 -- ui/ deploy/Dockerfile.ui` and the backend's `KUMO_GIT_SHA` from `HEAD` — DIFFERENT
 * revisions on purpose, because `deploy-ui-paper` ships the UI without touching the engine. That
 * comparison disagrees on every correctly-deployed stack. An alarm that fires on healthy state is one
 * this codebase has shipped twice (#387, #390) and paid for both times.
 *
 * So the comparison is bundle-vs-UI-SERVER instead: the id baked in at compile time against the one the
 * server reports at request time. Those diverge for exactly one reason — the browser is running
 * JavaScript from an earlier deploy — with no second cause to confuse it with.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { buildFreshness } from "./buildFreshness";

/** Real values, read from the deployed stack 2026-08-21. */
const RUNNING = "34c59cc-dirty";

describe("stale-bundle detection (#345 item 5)", () => {
  it("the fixture uses ids that genuinely differ", () => {
    // The fixture's own property first. Comparing a value against itself would pass with the whole
    // comparison deleted.
    expect(RUNNING).not.toBe("9c66c71");
  });

  it("same build is fresh", () => {
    expect(buildFreshness(RUNNING, RUNNING)).toEqual({ kind: "fresh" });
  });

  it("an OLDER running bundle is stale, and names both ids", () => {
    // Both ids in the payload because "reload" is the advice and "which build am I on" is the first
    // question anyone debugging this asks.
    expect(buildFreshness(RUNNING, "9c66c71")).toEqual({
      kind: "stale",
      running: RUNNING,
      serving: "9c66c71",
    });
  });

  it("a MISSING running id is unknown, NOT stale", () => {
    // A dev build has no baked id. Reporting it as stale would put a red banner on every local run,
    // and a banner that always shows is one nobody reads on the day it is right.
    expect(buildFreshness(null, "9c66c71").kind).toBe("unknown");
    expect(buildFreshness("", "9c66c71").kind).toBe("unknown");
    expect(buildFreshness("   ", "9c66c71").kind).toBe("unknown");
  });

  it("a MISSING served id is unknown, NOT stale", () => {
    // An offline phone, or a server built before `/api/build` existed. An unreachable check is not
    // evidence of anything, and must never be rendered as evidence.
    expect(buildFreshness(RUNNING, null).kind).toBe("unknown");
    expect(buildFreshness(RUNNING, undefined).kind).toBe("unknown");
    expect(buildFreshness(RUNNING, "").kind).toBe("unknown");
  });

  it("whitespace does not manufacture a mismatch", () => {
    // `.next/BUILD_ID` is read off disk and ends with a newline. Comparing it raw would report every
    // correctly-deployed stack as stale — the cry-wolf direction, arriving by a trailing "\\n".
    expect(buildFreshness(RUNNING, `${RUNNING}\n`)).toEqual({ kind: "fresh" });
    expect(buildFreshness(` ${RUNNING} `, RUNNING)).toEqual({ kind: "fresh" });
  });

  it("a DIRTY build is not the same as its clean commit", () => {
    // `34c59cc-dirty` and `34c59cc` are different code. Treating them as equal would hide exactly the
    // case where someone deployed a working tree and then deployed the real commit over it.
    expect(buildFreshness("34c59cc-dirty", "34c59cc").kind).toBe("stale");
  });
});

describe("the banner and the route are wired correctly (#345 item 5)", () => {
  // COMMENTS STRIPPED BEFORE MATCHING. Three assertions here first passed with the code deleted,
  // because they matched the file's own DOCSTRING — which explains `force-dynamic` and `no-store` in
  // prose. A source scan cannot tell code from the paragraph describing it, and this is the third time
  // that has bitten tonight. The mutation harness is what said so, each time.
  const strip = (src: string) => src.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n]*/g, " ");
  const BANNER = strip(readFileSync(join(import.meta.dirname, "..", "components", "board", "StaleBundleBanner.tsx"), "utf8"));
  const ROUTE = strip(readFileSync(join(import.meta.dirname, "..", "app", "api", "build", "route.ts"), "utf8"));
  const PAGE = strip(readFileSync(join(import.meta.dirname, "..", "app", "page.tsx"), "utf8"));

  it("the check is NOT cached — on both sides", () => {
    // A cached answer would be exactly as stale as the bundle it is meant to catch, and the check
    // would confirm its own staleness. This is the assertion that makes the feature real.
    expect(ROUTE).toMatch(/export const dynamic = "force-dynamic"/);
    expect(ROUTE).toMatch(/"cache-control":\s*"no-store/);
    expect(BANNER).toMatch(/cache:\s*"no-store"/);
  });

  it("it POLLS rather than checking once at mount", () => {
    // A tab left open across a deploy is the exact case: fresh when it loaded, stale under the
    // operator's feet with no interaction. A one-shot check answers the one moment it is always fresh.
    expect(BANNER).toMatch(/setInterval/);
  });

  it("the route falls back to the BUILD_ID file", () => {
    // Verified on the deployed image 2026-08-21: `BUILD_ID` was empty in the container and only
    // `/app/.next/BUILD_ID` carried the value. Env-only would return null on every image built before
    // the Dockerfile change that ships with this.
    expect(ROUTE).toMatch(/process\.env\.BUILD_ID/);
    // The CALL, not the import — deleting the read while leaving `import { readFileSync }` at the top
    // passed the first version of this assertion.
    expect(ROUTE).toMatch(/readFileSync\(p, "utf8"\)/);
  });

  it("the banner renders only when STALE", () => {
    expect(BANNER).toMatch(/state\.kind !== "stale"/);
  });

  it("it sits above the header, where it invalidates every number", () => {
    expect(PAGE).toMatch(/<StaleBundleBanner \/>/);
  });
});
