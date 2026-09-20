/** Alpaca's period vocabulary → the label on the tab. `all` is inception-to-date.
 *
 * A plain module rather than living beside the component: the test suite is `.ts` only, so a constant
 * exported from a `.tsx` file cannot be imported without dragging JSX through the parser. Splitting it
 * also says the right thing — the vocabulary is shared, the control is one renderer of it.
 */
export const PERIODS: Array<[key: string, label: string]> = [
  ["1D", "1D"],
  ["1W", "1W"],
  ["1M", "1M"],
  ["3M", "3M"],
  ["all", "All"],
];
