"use client";

import { useId } from "react";
import type { ModelCatalog } from "@/lib/api";

export default function ModelSelector({ value, onChange, models, catalog, label, allowDefault = false, compact = false }: {
  value: string;
  onChange: (model: string) => void;
  models: string[];
  catalog?: ModelCatalog;
  label: string;
  allowDefault?: boolean;
  compact?: boolean;
}) {
  const id = useId();
  const rows = catalog?.models ?? models.map(model => ({
    id: model, name: model, family: "Enabled models", lifecycle: "", selectable: true,
    access: "unverified", reason: "", note: "", retirement_date: "", documentation: "",
  }));
  const selected = rows.find(row => row.id === value);
  const missing = value && !selected;
  const families = [...new Set(rows.map(row => row.family))];
  return <div className={compact ? "model-selector-compact" : undefined}>
    <label htmlFor={id} style={{ display: "block", fontSize: 13, marginBottom: 6 }}>{label}</label>
    <select id={id} className="input" value={value} onChange={event => onChange(event.target.value)} aria-describedby={`${id}-help`}>
      {allowDefault && <option value="">{catalog ? `Server default (${catalog.default_model})` : "Saved default"}</option>}
      {!allowDefault && !value && <option value="" disabled>Choose a model</option>}
      {missing && <option value={value} disabled>{value} — no longer enabled</option>}
      {families.map(family => <optgroup key={family} label={family}>
        {rows.filter(row => row.family === family).map(row => <option key={row.id} value={row.id} disabled={!row.selectable}>
          {row.name}{!row.selectable ? " — unavailable" : ""}
        </option>)}
      </optgroup>)}
    </select>
    <details className={compact ? "model-selector-help" : undefined} open={compact ? undefined : true}>
    {compact && <summary>Model details{selected && !selected.selectable ? " · unavailable" : ""}</summary>}
    <p id={`${id}-help`} style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 6 }}>
      {selected?.reason || (missing ? "Choose an enabled model before starting work." : "Project access and quota are checked when the model runs.")}
      {selected?.note && selected.note !== selected.reason ? ` ${selected.note}` : ""}
      {selected?.retirement_date ? ` Retirement: ${selected.retirement_date}.` : ""}
      {selected?.documentation && <> <a href={selected.documentation} target="_blank" rel="noreferrer">Model details</a></>}
    </p>
    </details>
  </div>;
}
