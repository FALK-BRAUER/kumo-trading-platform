# board/grid

The react-grid-layout integration for the cockpit board (#7 P4).

- `GridImpl.tsx` — the actual `WidthProvider(GridLayout)` instance + grid constants. Touches `window`; never import directly.
- `GridShell.tsx` — `dynamic(ssr:false)` wrapper with a blank placeholder. **This** is what the Board imports.

Goes here: grid geometry, drag/resize plumbing, RGL prop wiring. Does NOT go here: tile content,
data binding, or layout persistence (that's `lib/framework/layout` + the store).
