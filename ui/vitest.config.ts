import { defineConfig } from "vitest/config";
import path from "node:path";

// Pure-logic unit tests (node env — no DOM). Risk-bearing modules (prefill sizing, auto-select) first.
//
// `oxc.jsx` is set so a test can IMPORT a module containing JSX. tsconfig says `jsx: "preserve"` (Next
// compiles it downstream), which left vitest unable to PARSE a .tsx at all — importing one failed with
// "invalid JS syntax" before a single test ran. That is why this repo had zero .test.tsx and why every UI
// fact has had to be asserted by scanning source text as a string, which is how two edge defects in the
// momentum map shipped green (#351).
//
// THE KEY IS `oxc`, NOT `esbuild`. Vite 8 transforms with rolldown/oxc, so the `esbuild: { jsx }` option
// every older answer reaches for is silently IGNORED — it does not error, the .tsx just keeps failing to
// parse, which reads exactly like "this cannot be done". Tried and abandoned once on that basis.
//
// This adds no DOM and no render stack: `environment` is still node, so a test may import a component
// module and assert on its definition, but not render it. Rendering needs jsdom + RTL, which is a
// separate decision.
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
  oxc: { jsx: { runtime: "automatic" } },
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
});
