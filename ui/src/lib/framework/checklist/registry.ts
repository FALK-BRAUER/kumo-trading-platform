/**
 * Checklist registry — named methodology definitions a tile binds to by id, mirroring
 * `datasource/registry.ts`'s shape exactly (`Map<name, T>`, populated by `@/config/*` registration
 * calls, core stays methodology-agnostic). #181.
 */
import type { ChecklistDef } from "./types";

const registry = new Map<string, ChecklistDef>();

export function registerChecklist(def: ChecklistDef): void {
  registry.set(def.id, def);
}

export function getChecklist(id: string): ChecklistDef | undefined {
  return registry.get(id);
}

export function allChecklists(): ChecklistDef[] {
  return [...registry.values()];
}
