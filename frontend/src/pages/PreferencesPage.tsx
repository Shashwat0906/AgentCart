import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Pencil, Trash2, X } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Container } from "../components/Layout";
import { useToast } from "../components/toastContext";
import { EmptyState, ErrorState, Notice, PageHeader, Spinner } from "../components/ui";
import { api } from "../lib/api";
import { CATEGORY_LABELS, formatINR } from "../lib/format";
import { errorMessage, qk } from "../lib/queries";
import type { Preference, PreferenceKind } from "../lib/types";

const KINDS: { value: PreferenceKind; label: string; placeholder: string }[] = [
  { value: "preferred_brand", label: "Preferred brand", placeholder: "e.g. Lenovo" },
  { value: "avoided_brand", label: "Brand to avoid", placeholder: "e.g. HP" },
  { value: "budget_max", label: "Usual budget (₹)", placeholder: "e.g. 60000" },
  { value: "use_case", label: "Typical use", placeholder: "e.g. machine learning" },
  { value: "note", label: "Other note", placeholder: "e.g. I prefer matte screens" },
];

function describe(p: Preference): string {
  const cat = p.category ? ` · ${CATEGORY_LABELS[p.category] ?? p.category}` : "";
  if (p.kind === "budget_max") return `Up to ${formatINR(Number(p.value))}${cat}`;
  return `${p.value}${cat}`;
}

function PreferenceRow({ pref }: { pref: Preference }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(pref.value);
  const refresh = () => qc.invalidateQueries({ queryKey: qk.preferences });
  const save = useMutation({ mutationFn: () => api.updatePreference(pref.id, { value }), onSuccess: () => { setEditing(false); refresh(); }, onError: (e) => toast(errorMessage(e), "error") });
  const del = useMutation({ mutationFn: () => api.deletePreference(pref.id), onSuccess: () => { refresh(); toast("Preference deleted"); }, onError: (e) => toast(errorMessage(e), "error") });
  const label = KINDS.find((k) => k.value === pref.kind)?.label ?? pref.kind;

  return (
    <li className="flex flex-wrap items-center gap-3 px-4 py-3">
      <span className="chip">{label}</span>
      {editing ? (
        <form className="flex flex-1 items-center gap-2" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
          <label className="sr-only" htmlFor={`pref-${pref.id}`}>Edit value</label>
          <input id={`pref-${pref.id}`} className="input py-1.5" value={value} maxLength={200} onChange={(e) => setValue(e.target.value)} autoFocus />
          <button className="btn btn-approve btn-sm" disabled={save.isPending || !value.trim()} aria-label="Save"><Check size={14} /></button>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => { setEditing(false); setValue(pref.value); }} aria-label="Cancel"><X size={14} /></button>
        </form>
      ) : (
        <span className="flex-1 font-medium">{describe(pref)}</span>
      )}
      <span className="text-xs text-muted">{pref.source === "agent" ? "Saved via assistant (approved)" : "Added by you"}</span>
      {!editing && (
        <div className="flex gap-1">
          <button className="rounded-lg p-2 text-muted hover:bg-paper-2 hover:text-ink" onClick={() => setEditing(true)} aria-label={`Edit ${label}`}><Pencil size={15} /></button>
          <button className="rounded-lg p-2 text-muted hover:bg-danger-soft hover:text-danger" onClick={() => del.mutate()} aria-label={`Delete ${label}`}><Trash2 size={15} /></button>
        </div>
      )}
    </li>
  );
}

export function PreferencesPage() {
  const prefs = useQuery({ queryKey: qk.preferences, queryFn: api.preferences });
  const qc = useQueryClient();
  const toast = useToast();
  const [kind, setKind] = useState<PreferenceKind>("preferred_brand");
  const [value, setValue] = useState("");
  const [category, setCategory] = useState("");
  const create = useMutation({
    mutationFn: () => api.createPreference({ kind, value: value.trim(), category: category || null }),
    onSuccess: () => { setValue(""); qc.invalidateQueries({ queryKey: qk.preferences }); toast("Preference saved"); },
  });
  const meta = KINDS.find((k) => k.value === kind)!;

  function submit(e: FormEvent) {
    e.preventDefault();
    if (value.trim()) create.mutate();
  }

  return (
    <Container className="max-w-3xl">
      <PageHeader eyebrow="Memory" title="Saved preferences" />
      <p className="-mt-3 mb-6 text-muted">
        The assistant uses these as gentle defaults — they never override what you ask for in a conversation.
        Only you can add them, or approve the assistant's suggestions. Personal data such as phone numbers, emails or card numbers is refused.
      </p>
      <form onSubmit={submit} className="card mb-6 grid gap-3 p-4 sm:grid-cols-[180px_1fr_160px_auto] sm:items-end">
        <div>
          <label className="label" htmlFor="kind">Type</label>
          <select id="kind" className="input" value={kind} onChange={(e) => setKind(e.target.value as PreferenceKind)}>
            {KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="value">Value</label>
          <input id="value" className="input" placeholder={meta.placeholder} value={value} maxLength={200} inputMode={kind === "budget_max" ? "numeric" : undefined} onChange={(e) => setValue(e.target.value)} />
        </div>
        <div>
          <label className="label" htmlFor="pcat">Category {kind === "budget_max" ? "" : "(optional)"}</label>
          <select id="pcat" className="input" value={category} onChange={(e) => setCategory(e.target.value)} required={kind === "budget_max"}>
            <option value="">Any</option>
            {Object.entries(CATEGORY_LABELS).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
          </select>
        </div>
        <button className="btn btn-primary" disabled={create.isPending || !value.trim()}>Save</button>
        {create.isError && <div className="sm:col-span-4"><Notice tone="danger">{errorMessage(create.error)}</Notice></div>}
      </form>
      {prefs.isLoading ? <Spinner /> : prefs.isError ? <ErrorState error={prefs.error} /> : !prefs.data?.length ? (
        <EmptyState title="No saved preferences">Tell the assistant something like “remember that I prefer Lenovo” and approve the suggestion, or add one above.</EmptyState>
      ) : (
        <ul className="card divide-y divide-line">{prefs.data.map((p) => <PreferenceRow key={p.id} pref={p} />)}</ul>
      )}
    </Container>
  );
}
