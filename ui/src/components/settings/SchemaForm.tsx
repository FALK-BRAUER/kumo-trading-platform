"use client";

/**
 * SchemaForm (#49) — renders a settings form DYNAMICALLY from a JSON Schema. No per-setting UI: each
 * property maps by type (enum→select, boolean→toggle, number/integer→bounded input, string→text). Values
 * are validated authoritatively by the backend on save (422 → per-field errors shown); this is the render +
 * edit surface. Hand-rolled (small flat schemas, all-Tailwind UI) — no form library.
 */
import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { JsonSchema, JsonSchemaField, SettingsValues } from "@/lib/api/client";
import { stripEmpty } from "@/lib/settingsForm";

function Field({
  name,
  field,
  value,
  onChange,
}: {
  name: string;
  field: JsonSchemaField;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const label = field.title ?? name;
  const inputCls =
    "bg-ds-surf2 text-t1 text-xs font-mono px-2 py-1 rounded w-full border border-ds-line2 focus:border-status-info outline-none";

  let control;
  if (field.enum) {
    control = (
      <select className={inputCls} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)}>
        {field.enum.map((opt) => (
          <option key={opt} value={opt}>
            {opt}
          </option>
        ))}
      </select>
    );
  } else if (field.type === "boolean") {
    control = (
      <button
        type="button"
        onClick={() => onChange(!value)}
        className={`px-2 py-0.5 rounded text-[11px] font-mono font-bold transition-colors ${
          value ? "bg-status-bull/15 text-status-bull ring-1 ring-status-bull/40" : "bg-ds-surf2 text-t2"
        }`}
      >
        {value ? "ON" : "OFF"}
      </button>
    );
  } else if (field.type === "number" || field.type === "integer") {
    control = (
      <input
        type="number"
        className={inputCls}
        value={value == null ? "" : String(value)}
        min={field.minimum}
        max={field.maximum}
        step={field.type === "integer" ? 1 : "any"}
        onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
      />
    );
  } else if (field.type === "object") {
    // READ-ONLY, deliberately (#872). This form maps a property to a control BY TYPE and had no object
    // branch, so a nested object fell through to the text input below and rendered "[object Object]" —
    // and one keystroke would have replaced the whole object with that string, which the backend then
    // refuses with a 422 that blocks saving anything else in the domain.
    //
    // Not given a real editor because these values are INSTANCE CONFIG: `make up` re-seeds
    // `settings/*.json` from the instances repo over any PUT, so an edit made here does not survive the
    // next deploy. Showing the effective value and saying where it is set is the honest surface.
    control = (
      <pre
        className={`${inputCls} overflow-x-auto whitespace-pre-wrap`}
        title="Set in the kumo-cockpit-instances repo. A deploy re-seeds this file, so an edit here would not survive it."
      >
        {JSON.stringify(value ?? {}, null, 1)}
      </pre>
    );
  } else {
    control = (
      <input type="text" className={inputCls} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} />
    );
  }

  return (
    <label className="flex flex-col gap-1">
      <span className="font-mono text-[11px] font-semibold uppercase tracking-wider text-t2">{label}</span>
      {control}
      {field.description && <span className="font-mono text-[10px] text-t3">{field.description}</span>}
    </label>
  );
}

export function SchemaForm({
  domain,
  schema,
  values,
  onSave,
}: {
  domain: string;
  schema: JsonSchema;
  values: SettingsValues;
  onSave: (domain: string, values: SettingsValues) => Promise<{ values: SettingsValues }>;
}) {
  const [form, setForm] = useState<SettingsValues>(values);
  const [dirty, setDirty] = useState(false);
  // Sync to fresh server values on a refetch, but never stomp an in-progress edit.
  useEffect(() => {
    if (!dirty) setForm(values);
  }, [values, dirty]);
  const qc = useQueryClient();
  const save = useMutation({
    // Drop cleared fields so the backend fills defaults (a null numeric would 422).
    mutationFn: () => onSave(domain, stripEmpty(form)),
    onSuccess: (res) => {
      setForm(res.values);
      setDirty(false);
      // Other screens read a domain's values through `["settings", domain]` — PEAK's arm parameters do
      // exactly that. Without this the toggle keeps serving a cached, pre-save response and arms a
      // position with the trail widths the operator just changed away from. (codex review, High.)
      //
      // WRITE the saved values in, don't only invalidate: react-query keeps serving the old data (still
      // `success`) for the whole refetch, so an invalidate alone leaves a window where the toggle reads
      // ready-and-stale. The PUT response already carries the resolved values, so there is nothing to
      // wait for. The refetch afterwards is just reconciliation. (codex review, round 2, High.)
      qc.setQueryData(["settings", domain], (prev: unknown) =>
        prev && typeof prev === "object" ? { ...prev, values: res.values } : prev,
      );
      qc.invalidateQueries({ queryKey: ["settings", domain] });
    },
  });
  const props = schema.properties ?? {};

  return (
    <div className="rounded-xl border border-ds-line bg-ds-surf/40 p-4">
      <div className="mb-3">
        <h3 className="font-mono text-sm font-bold text-t1">{schema.title ?? domain}</h3>
        {schema.description && <p className="mt-0.5 font-mono text-[10px] text-t3">{schema.description}</p>}
      </div>
      <div className="mb-3 flex flex-col gap-3">
        {Object.entries(props).map(([name, field]) => (
          <Field
            key={name}
            name={name}
            field={field}
            value={form[name]}
            onChange={(v) => {
              setForm((f) => ({ ...f, [name]: v }));
              setDirty(true);
            }}
          />
        ))}
      </div>
      <button
        onClick={() => save.mutate()}
        disabled={!dirty || save.isPending}
        className="rounded bg-status-info px-3 py-1 font-mono text-[11px] font-bold text-ds-bg transition-colors hover:bg-status-info/90 disabled:cursor-not-allowed disabled:opacity-40"
      >
        {save.isPending ? "Saving…" : save.isSuccess && !dirty ? "Saved ✓" : "Save"}
      </button>
      {save.isError && <div className="mt-2 font-mono text-[10px] text-status-bear">{String(save.error)}</div>}
    </div>
  );
}
