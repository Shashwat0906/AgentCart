import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Clock, Hand, ShieldCheck, X } from "lucide-react";
import { useState } from "react";
import { useToast } from "../../components/toastContext";
import { api, ApiError } from "../../lib/api";
import { formatINR } from "../../lib/format";
import { errorMessage, qk } from "../../lib/queries";
import type { AgentAction } from "../../lib/types";

const STATUS: Record<AgentAction["status"], { label: string; cls: string }> = {
  pending: { label: "Waiting for your approval", cls: "border-accent/30 bg-accent-soft text-accent-strong" },
  executed: { label: "Approved & done", cls: "border-leaf/25 bg-leaf-soft text-leaf" },
  rejected: { label: "Rejected", cls: "border-line bg-paper-2 text-muted" },
  failed: { label: "Couldn't complete", cls: "border-danger/30 bg-danger-soft text-danger" },
  expired: { label: "Expired", cls: "border-line bg-paper-2 text-muted" },
  superseded: { label: "Replaced by a newer proposal", cls: "border-line bg-paper-2 text-muted" },
};

/** The per-item budget a task recorded with its proposal (budgets apply to each item, not the cart total). */
function budgetLabel(payload: Record<string, unknown>): string | null {
  const max = typeof payload.budget_max_inr === "number" ? payload.budget_max_inr : null;
  const min = typeof payload.budget_min_inr === "number" ? payload.budget_min_inr : null;
  if (max !== null && min !== null) return `Your budget: ${formatINR(min)}–${formatINR(max)} per item`;
  if (max !== null) return `Your budget: up to ${formatINR(max)} per item`;
  if (min !== null) return `Your budget: at least ${formatINR(min)} per item`;
  return null;
}

export function ApprovalCard({
  action,
  conversationId,
  onDecided,
}: {
  action: AgentAction;
  conversationId?: string;
  /** Called after an approve or reject attempt, so the owner (e.g. a task card) can refresh. */
  onDecided?: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const [current, setCurrent] = useState(action);
  const [problem, setProblem] = useState<string | null>(null);
  const [replacement, setReplacement] = useState<{ id: string; price: number } | null>(null);

  const refresh = () => {
    if (conversationId) qc.invalidateQueries({ queryKey: qk.conversation(conversationId) });
    onDecided?.();
  };

  const approve = useMutation({
    mutationFn: (id: string) => api.approve(id),
    onSuccess: (res) => {
      setCurrent(res.action);
      setReplacement(null);
      setProblem(null);
      if (res.cart) qc.setQueryData(qk.cart, res.cart);
      if (res.action.kind === "save_preference") qc.invalidateQueries({ queryKey: qk.preferences });
      toast(res.action.kind === "add_to_cart" ? "Added to your cart" : "Preference saved");
      refresh();
    },
    onError: (err) => {
      if (err instanceof ApiError && err.details.reason === "price_changed" && typeof err.details.new_action_id === "string") {
        setCurrent((c) => ({ ...c, status: "superseded" }));
        setReplacement({ id: err.details.new_action_id, price: Number(err.details.new_price_inr) });
        setProblem(err.message);
      } else if (err instanceof ApiError && (err.status === 409 || err.status === 410)) {
        setProblem(err.message);
        setCurrent((c) => ({ ...c, status: err.status === 410 ? ((err.details.status as AgentAction["status"]) ?? "expired") : "failed" }));
      } else {
        toast(errorMessage(err), "error");
      }
      refresh();
    },
  });
  const reject = useMutation({
    mutationFn: () => api.reject(current.id),
    onSuccess: (res) => { setCurrent(res.action); refresh(); },
    onError: (err) => setProblem(errorMessage(err)),
  });

  const status = STATUS[current.status];
  const busy = approve.isPending || reject.isPending;
  return (
    <section className="rounded-2xl border-2 border-dashed border-accent/40 bg-white p-4" aria-label="Proposed action">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="flex items-center gap-2 text-sm font-semibold"><Hand size={16} className="text-accent" aria-hidden /> The assistant is asking permission</p>
        <span className={`chip ${status.cls}`} data-testid="action-status">{status.label}</span>
      </div>
      <p className="mt-2 font-medium">{current.summary}</p>
      {budgetLabel(current.payload) && (
        <p className="mt-1 text-xs text-muted" data-testid="action-budget">{budgetLabel(current.payload)}</p>
      )}
      {current.status === "pending" && (
        <>
          <p className="mt-1 flex items-center gap-1.5 text-xs text-muted"><ShieldCheck size={13} aria-hidden /> Nothing changes until you approve. Price and stock are re-checked at that moment. <Clock size={13} aria-hidden /> Expires {new Date(current.expires_at).toLocaleTimeString("en-IN", { timeStyle: "short" })}.</p>
          <div className="mt-3 flex gap-2">
            <button className="btn btn-approve btn-sm" onClick={() => approve.mutate(current.id)} disabled={busy}><Check size={14} aria-hidden /> Approve</button>
            <button className="btn btn-ghost btn-sm" onClick={() => reject.mutate()} disabled={busy}><X size={14} aria-hidden /> Reject</button>
          </div>
        </>
      )}
      {problem && <p className="mt-2 text-sm text-warn" role="alert">{problem}</p>}
      {replacement && (
        <div className="mt-3 flex flex-wrap items-center gap-2 rounded-xl bg-warn-soft p-3 text-sm">
          <span>New price: <strong>{formatINR(replacement.price)}</strong>. Approve again at this price?</span>
          <button className="btn btn-approve btn-sm" onClick={() => approve.mutate(replacement.id)} disabled={busy}>Approve at new price</button>
        </div>
      )}
    </section>
  );
}
