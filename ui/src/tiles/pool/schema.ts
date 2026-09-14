/**
 * Pool tile config. Split from `definition.ts` (which imports the component) so pure-logic consumers
 * and node-environment tests can import the schema without pulling JSX through.
 */
import { z } from "zod";

export const POOL_FILTERS = ["all", "held", "whitelist", "blacklist"] as const;

export const poolConfigSchema = z.object({
  /** Which view the tile opens on. A stale/corrupt value degrades to "all", never to an impossible filter. */
  filter: z.enum(POOL_FILTERS).default("all"),
});

export type PoolConfig = z.infer<typeof poolConfigSchema>;
