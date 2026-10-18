import { Bot, Cpu, Info, ShieldAlert, Sparkles, Workflow } from "lucide-react";
import { ComparisonTable } from "../../components/ComparisonTable";
import { Notice } from "../../components/ui";
import { splitBold } from "../../lib/format";
import type { ChatMessage } from "../../lib/types";
import { ApprovalCard } from "./ApprovalCard";
import { RecommendationCard } from "./RecommendationCard";

const NODE_LABELS: Record<string, string> = {
  parse_request: "Parse request",
  extract_constraints: "Extract constraints",
  validate_constraints: "Validate constraints",
  clarify: "Ask clarifying question",
  search_catalog: "Search catalogue",
  rank_products: "Filter & rank",
  explain_recommendations: "Explain",
  compare_products: "Compare",
  resolve_reference: "Resolve reference",
  respond_simple: "Respond",
  propose_actions: "Propose actions",
  await_approval: "Await approval",
  safe_failure: "Safe failure",
};

function RichText({ text }: { text: string }) {
  return <>{splitBold(text).map((p, i) => (p.bold ? <strong key={i}>{p.text}</strong> : <span key={i}>{p.text}</span>))}</>;
}

export function MessageView({ message, conversationId, onQuickReply }: { message: ChatMessage; conversationId?: string; onQuickReply: (text: string) => void }) {
  if (message.role === "user") {
    return (
      <div className="flex animate-rise justify-end">
        <p className="max-w-[85%] rounded-2xl rounded-br-md bg-ink px-4 py-2.5 whitespace-pre-wrap text-paper">{message.content}</p>
      </div>
    );
  }
  const p = message.payload ?? {};
  const recs = p.recommendations ?? [];
  const alts = p.alternatives ?? [];
  const showComparisonTable = p.comparison && (p.type === "comparison" || recs.length >= 2);

  return (
    <div className="flex animate-rise gap-3" data-testid="assistant-message">
      <span className="mt-1 grid h-8 w-8 shrink-0 place-items-center rounded-full bg-accent-soft text-accent" aria-hidden><Bot size={17} /></span>
      <div className="min-w-0 flex-1 space-y-3">
        {(p.security_flags?.length ?? 0) > 0 && (
          <Notice tone="warn" title="Instruction-like text detected"><span className="inline-flex items-center gap-1"><ShieldAlert size={14} aria-hidden /> Treated as plain text — rules, prices and approvals are unchanged.</span></Notice>
        )}
        {(p.understood?.length ?? 0) > 0 && (
          <div>
            <p className="eyebrow mb-1.5 flex items-center gap-1.5"><Sparkles size={12} aria-hidden /> What I understood</p>
            <ul className="flex flex-wrap gap-1.5">
              {p.understood!.map((u) => (
                <li key={u} className={`chip ${u.endsWith("(required)") ? "border-ink/20 bg-white font-semibold" : ""}`}>{u}</li>
              ))}
            </ul>
          </div>
        )}
        {(p.applied_preferences?.length ?? 0) > 0 && (
          <p className="flex items-center gap-1.5 text-xs text-muted"><Info size={13} aria-hidden /> Using your saved preferences: {p.applied_preferences!.join(" · ")}</p>
        )}
        <div className="card px-4 py-3 leading-relaxed" data-testid="assistant-summary"><RichText text={message.content} /></div>

        {(p.notices ?? []).filter((n) => !n.startsWith("Part of your message")).map((n) => <Notice key={n} tone={/out of stock|limit|only|can't|won't|didn't|Ignored/i.test(n) ? "warn" : "info"}>{n}</Notice>)}

        {(p.questions?.length ?? 0) > 0 && (
          <div className="space-y-2">
            {p.questions!.map((q) => (
              <div key={q.id} className="rounded-xl border border-info/25 bg-info-soft p-3">
                {!message.content.includes(q.question) && <p className="text-sm font-semibold text-info">{q.question}</p>}
                {q.options.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {q.options.slice(0, 9).map((o) => <button key={o} className="chip bg-white hover:border-info hover:text-info" onClick={() => onQuickReply(q.id === "category" ? `I'm looking for ${o.toLowerCase()}` : o)}>{o}</button>)}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {(p.actions ?? []).map((a) => <ApprovalCard key={a.id} action={a} conversationId={conversationId} />)}

        {p.type !== "comparison" && recs.length > 0 && <div className="space-y-3">{recs.map((r) => <RecommendationCard key={`${r.product_id}-${r.rank}`} card={r} />)}</div>}
        {alts.length > 0 && <div className="space-y-3">{alts.map((r) => <RecommendationCard key={`alt-${r.product_id}`} card={r} />)}</div>}

        {showComparisonTable && (
          <details className="group" open={p.type === "comparison"}>
            <summary className="cursor-pointer text-sm font-semibold text-ink-2 hover:text-ink">Compare {p.type === "comparison" ? "" : "the top picks "}side by side</summary>
            <div className="mt-2"><ComparisonTable data={p.comparison!} /></div>
          </details>
        )}

        {(p.trace?.length ?? 0) > 0 && (
          <details className="text-xs text-muted">
            <summary className="inline-flex cursor-pointer items-center gap-1.5 hover:text-ink"><Workflow size={13} aria-hidden /> How I got here ({p.trace!.length} steps · {Math.round(p.trace!.reduce((s, t) => s + t.ms, 0))} ms · {p.provider === "mock" ? "rule-based" : p.provider}{p.llm_used ? " + LLM" : ""})</summary>
            <ol className="mt-2 space-y-1 rounded-xl border border-line bg-white p-3">
              {p.trace!.map((t, i) => (
                <li key={i} className="flex flex-wrap gap-x-2">
                  <span className={`font-semibold ${t.status === "error" ? "text-danger" : "text-ink-2"}`}>{i + 1}. {NODE_LABELS[t.node] ?? t.node}</span>
                  <span className="tabular-nums">{t.ms} ms</span>
                  {t.detail && <span className="text-muted">— {t.detail}</span>}
                </li>
              ))}
              {(p.tool_calls ?? []).map((c, i) => (
                <li key={`tool-${i}`} className="flex items-center gap-1.5 text-muted"><Cpu size={12} aria-hidden /> tool <code>{c.tool}</code> {c.ok ? "✓" : "✗"} {c.result_count ?? ""} results{c.attempts > 1 ? ` after ${c.attempts} attempts` : ""}</li>
              ))}
            </ol>
          </details>
        )}
      </div>
    </div>
  );
}
