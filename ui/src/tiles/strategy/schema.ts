/**
 * Strategy tile config. Split from `definition.ts` (which imports the component) so pure-logic
 * consumers and node-environment tests can import the schema without pulling JSX through.
 */
import { z } from "zod";

export const strategyConfigSchema = z.object({
  /** Which strategy the tile describes. One automated strategy exists today; the frame names it. */
  strategyId: z.string().default("MOMENTUM-002"),
});

export type StrategyConfig = z.infer<typeof strategyConfigSchema>;
