# book/

The Book tile — the account's P&L split per strategy: a net header (standing unrealized, realized over
the selected window) and one cell per StrategyId with that lane's held count, headline, sleeve
percentage, secured value and, since #888, its cadence beside the liveness figures.

- `BookTile.tsx` — the panel and `BookCell`; `definition.ts` — tile type and data sources.
- `periodNet.ts`, `securedSub.ts`, `panelIdentity.ts`, `cadenceNote.ts` — pure helpers the cells render;
  each has its test beside it, and the `*Seam*`/`*Wiring*`/`bookCellHeadline` tests drive the real
  component through `managed-portfolio/cellParser.fixture.ts`.

Not here: the per-instrument drill-down (`managed-portfolio/`), the hooks that fetch `/strategies`
(`lib/framework/useSleeves.ts`, `useLaneCadence.ts`), and the headline rule shared with the managed
tile (`cellHeadline` in `managed-portfolio/books.ts`).
