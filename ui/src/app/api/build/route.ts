/**
 * What build this server is serving, RIGHT NOW (#345 item 5).
 *
 * `force-dynamic` + `no-store` are the whole point: a cached answer would be exactly as stale as the
 * bundle it is supposed to catch, and the check would confirm its own staleness.
 *
 * Reads the env first, then `.next/BUILD_ID` on disk. Both exist deliberately — `Dockerfile.ui`'s run
 * stage declares `ARG BUILD_ID` for the image LABEL but never exported it as an ENV, so on the currently
 * deployed image only the file answers (verified 2026-08-21: `BUILD_ID` empty in the container,
 * `/app/.next/BUILD_ID` reads `34c59cc-dirty`). The file is written by Next at build time and copied
 * into the standalone output, so it is authoritative even when nothing else is set.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

export const dynamic = "force-dynamic";
export const revalidate = 0;

function servedBuildId(): string | null {
  const fromEnv = (process.env.BUILD_ID ?? "").trim();
  if (fromEnv) return fromEnv;
  for (const p of [join(process.cwd(), ".next", "BUILD_ID"), join(process.cwd(), "BUILD_ID")]) {
    try {
      const v = readFileSync(p, "utf8").trim();
      if (v) return v;
    } catch {
      // Not there — try the next candidate. A missing file is "unknown", never an error page: this
      // endpoint must not be able to take the app down.
    }
  }
  return null;
}

export function GET() {
  return new Response(JSON.stringify({ buildId: servedBuildId() }), {
    headers: { "content-type": "application/json", "cache-control": "no-store, max-age=0" },
  });
}
