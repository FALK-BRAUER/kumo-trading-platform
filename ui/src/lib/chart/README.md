# lib/chart

Chart rendering helpers for the `lightweight-charts` ChartTile — custom series + pure geometry, no React.

- `cloudSeries.ts` — the Ichimoku **kumo (cloud)** as a v5 custom series (`ICustomSeriesPaneView`).
  lightweight-charts has no native fill-between-two-lines, so a custom pane renderer fills the band between
  Senkou Span A and Span B, coloured bull (green, A≥B) / bear (red, B>A), splitting the fill at the exact
  crossover. `buildCloudData(spanA, spanB)` zips the two span line-series (by time) into the cloud's points.
- `*.test.ts` — the pure helpers (the canvas renderer is verified visually).

Goes here: chart-specific custom series / renderers / data-shaping for ChartTile.
Does NOT go here: the tile component itself (`@/components/tiles/ChartTile`) or generic indicators
(Ichimoku line math lives in `ChartTile`; order-level indicators in `@/lib/order/indicators`).
