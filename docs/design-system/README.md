# docs/design-system

The kumo-trading-platform design system — the versioned source of the work developed interactively (Perplexity + Codex
synthesis, IBKR/moomoo inspiration). This is the design ANCHOR; the tracking epic is **GH #104**.

- `STYLE_GUIDE.md` — the spec: tokens, primitives, the row/format contract, tile/tab framework, flexible
  (strategy-adaptive) composition, component registry, semi-auto UX, safety, responsiveness, migration plan.
  Kept in sync with issue #104's body.
- `mock-v2.html` — the standalone reference mock (self-contained HTML, day/night). Open it directly, or serve
  it (`python3 -m http.server` in this dir). Every surface + state + primitive rendered.
- `ROADMAP.md` — phased implementation plan + which surfaces map to existing tickets vs new ones.

Current surfaces in the mock: board chrome · watchlist · orders · positions · detail · order ticket ·
settings · reconciliation · analytics · component registry · **systematic strategy (status · book · pool ·
action log, #193)**.

Goes here: design specs, the reference mock, design decisions. Does NOT go here: implemented component code
(that lives in `ui/src/components/ds/` once built) or per-tile code.
