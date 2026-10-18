import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, MessageSquarePlus, Send, SlidersHorizontal, Trash2 } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { useToast } from "../../components/toastContext";
import { ErrorState, Notice } from "../../components/ui";
import { api } from "../../lib/api";
import { relativeTime } from "../../lib/format";
import { errorMessage, qk } from "../../lib/queries";
import type { ChatMessage, ConversationDetail, Weights } from "../../lib/types";
import { MessageView } from "./MessageView";

const DEFAULT_WEIGHTS: Weights = { budget: 0.25, spec: 0.3, rating: 0.2, preference: 0.25 };
const WEIGHT_LABELS: Record<keyof Weights, string> = { budget: "Budget fit", spec: "Spec match", rating: "Ratings", preference: "Your preferences" };
const STARTERS = [
  "I need a laptop under ₹70,000 for machine learning and college work, with 16 GB RAM, good battery life, and upgradeable storage.",
  "Phone between 20k and 30k with a great camera",
  "Noise cancelling headphones under 10000 for travel",
  "Compare Sony WH-1000XM5 vs Bose QuietComfort Ultra",
];
const WORKFLOW = ["Understand", "Validate", "Search", "Rank", "Explain"];

function loadWeights(): Weights {
  try {
    const raw = localStorage.getItem("agentcart.weights");
    return raw ? { ...DEFAULT_WEIGHTS, ...(JSON.parse(raw) as Partial<Weights>) } : DEFAULT_WEIGHTS;
  } catch {
    return DEFAULT_WEIGHTS;
  }
}

function WeightsPanel({ weights, onChange }: { weights: Weights; onChange: (w: Weights) => void }) {
  const total = Object.values(weights).reduce((a, b) => a + b, 0) || 1;
  return (
    <div className="card space-y-3 p-4" aria-label="Ranking weights">
      <p className="text-sm font-semibold">How should I rank?</p>
      <p className="text-xs text-muted">Hard requirements are always enforced first. These weights only decide the order of products that qualify.</p>
      {(Object.keys(weights) as (keyof Weights)[]).map((k) => (
        <div key={k}>
          <label htmlFor={`w-${k}`} className="flex justify-between text-xs font-medium"><span>{WEIGHT_LABELS[k]}</span><span className="tabular-nums text-muted">{Math.round((weights[k] / total) * 100)}%</span></label>
          <input id={`w-${k}`} type="range" min={0} max={1} step={0.05} value={weights[k]} onChange={(e) => onChange({ ...weights, [k]: Number(e.target.value) })} className="w-full accent-[var(--color-accent)]" />
        </div>
      ))}
      <button className="btn btn-ghost btn-sm w-full" onClick={() => onChange(DEFAULT_WEIGHTS)}>Reset to defaults</button>
    </div>
  );
}

export function ChatPage() {
  const { conversationId } = useParams();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const toast = useToast();
  const [input, setInput] = useState("");
  const [weights, setWeights] = useState<Weights>(loadWeights);
  const [showWeights, setShowWeights] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const autoSent = useRef(false);

  const conversations = useQuery({ queryKey: qk.conversations, queryFn: api.conversations });
  const conversation = useQuery({ queryKey: qk.conversation(conversationId ?? ""), queryFn: () => api.conversation(conversationId!), enabled: !!conversationId });

  useEffect(() => {
    try { localStorage.setItem("agentcart.weights", JSON.stringify(weights)); } catch { /* storage unavailable */ }
  }, [weights]);

  const send = useMutation({
    mutationFn: (message: string) => api.chat({ message, conversation_id: conversationId, weights }),
    onMutate: (message) => setPending(message),
    onSuccess: (res) => {
      qc.setQueryData<ConversationDetail>(qk.conversation(res.conversation_id), (old) => ({
        id: res.conversation_id,
        title: res.title,
        created_at: old?.created_at ?? res.user_message.created_at,
        updated_at: res.assistant_message.created_at,
        messages: [...(old?.messages ?? []), res.user_message, res.assistant_message],
      }));
      qc.invalidateQueries({ queryKey: qk.conversations });
      if (res.conversation_id !== conversationId) navigate(`/assistant/${res.conversation_id}`, { replace: !conversationId });
    },
    onError: (err) => toast(errorMessage(err), "error"),
    onSettled: () => setPending(null),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteConversation(id),
    onSuccess: (_, id) => {
      qc.invalidateQueries({ queryKey: qk.conversations });
      qc.removeQueries({ queryKey: qk.conversation(id) });
      if (id === conversationId) navigate("/assistant");
    },
  });

  const { mutate: sendMessage } = send;
  // Pre-filled question from the home page (?q=...). Sent once.
  useEffect(() => {
    const q = params.get("q");
    if (q && !autoSent.current && !conversationId) {
      autoSent.current = true;
      setParams({}, { replace: true });
      sendMessage(q);
    }
  }, [params, conversationId, setParams, sendMessage]);

  const messages: ChatMessage[] = conversationId ? conversation.data?.messages ?? [] : [];
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length, pending]);

  function submit(e?: FormEvent) {
    e?.preventDefault();
    const text = input.trim();
    if (!text || send.isPending) return;
    setInput("");
    send.mutate(text);
  }

  return (
    <div className="mx-auto grid max-w-7xl gap-6 px-4 py-6 sm:px-6 lg:grid-cols-[260px_1fr]">
      <aside className="hidden space-y-4 self-start lg:sticky lg:top-20 lg:block lg:max-h-[calc(100dvh-6rem)] lg:overflow-y-auto" aria-label="Conversations">
        <Link to="/assistant" className="btn btn-primary w-full"><MessageSquarePlus size={16} aria-hidden /> New conversation</Link>
        <nav aria-label="Conversation history">
          <p className="eyebrow mb-2">History</p>
          {conversations.isLoading && <div className="space-y-2">{[1, 2, 3].map((i) => <div key={i} className="skeleton h-10" />)}</div>}
          {conversations.data?.length === 0 && <p className="text-sm text-muted">No conversations yet.</p>}
          <ul className="space-y-1">
            {conversations.data?.map((c) => (
              <li key={c.id} className={`group flex items-center rounded-lg ${c.id === conversationId ? "bg-paper-2" : "hover:bg-paper-2/60"}`}>
                <Link to={`/assistant/${c.id}`} className="min-w-0 flex-1 px-3 py-2" aria-current={c.id === conversationId ? "page" : undefined}>
                  <p className="truncate text-sm font-medium">{c.title}</p>
                  <p className="text-xs text-muted">{relativeTime(c.updated_at)} · {c.message_count} messages</p>
                </Link>
                <button className="mr-1 rounded p-1.5 text-muted opacity-0 group-hover:opacity-100 hover:text-danger focus:opacity-100" onClick={() => remove.mutate(c.id)} aria-label={`Delete conversation ${c.title}`}><Trash2 size={14} /></button>
              </li>
            ))}
          </ul>
        </nav>
        <WeightsPanel weights={weights} onChange={setWeights} />
      </aside>

      <section className="flex min-h-[calc(100dvh-10rem)] flex-col" aria-label="Shopping assistant chat">
        <div className="mb-4 flex items-center justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold">{conversation.data?.title ?? "Shopping assistant"}</h1>
            <p className="text-sm text-muted">Grounded in the live catalogue. Proposes — never performs — cart changes.</p>
          </div>
          <button className="btn btn-ghost btn-sm lg:hidden" onClick={() => setShowWeights((s) => !s)} aria-expanded={showWeights}><SlidersHorizontal size={14} aria-hidden /> Ranking</button>
        </div>
        {showWeights && <div className="mb-4 lg:hidden"><WeightsPanel weights={weights} onChange={setWeights} /></div>}

        <div className="flex-1 space-y-6" aria-live="polite" aria-busy={send.isPending}>
          {conversationId && conversation.isError && <ErrorState error={conversation.error} onRetry={() => conversation.refetch()} />}
          {!conversationId && !pending && (
            <div className="card p-6">
              <div className="flex items-center gap-3"><span className="grid h-10 w-10 place-items-center rounded-full bg-accent-soft text-accent"><Bot size={20} aria-hidden /></span><p className="font-display text-xl font-semibold">What are you shopping for?</p></div>
              <p className="mt-2 text-sm text-muted">Describe it like you would to a friend — budget, must-haves and nice-to-haves. I'll show what I understood, why each pick fits and what you'd be trading off.</p>
              <div className="mt-4 grid gap-2 sm:grid-cols-2">
                {STARTERS.map((s) => <button key={s} className="rounded-xl border border-line bg-paper p-3 text-left text-sm hover:border-accent hover:bg-white" onClick={() => send.mutate(s)}>{s}</button>)}
              </div>
            </div>
          )}
          {messages.map((m) => <MessageView key={m.id} message={m} conversationId={conversationId} onQuickReply={(t) => send.mutate(t)} />)}
          {pending && (
            <>
              <div className="flex justify-end"><p className="max-w-[85%] rounded-2xl rounded-br-md bg-ink/80 px-4 py-2.5 text-paper">{pending}</p></div>
              <div className="flex gap-3" role="status">
                <span className="mt-1 grid h-8 w-8 place-items-center rounded-full bg-accent-soft text-accent" aria-hidden><Bot size={17} /></span>
                <div className="card px-4 py-3 text-sm text-muted">
                  <span className="sr-only">Assistant is working</span>
                  <span className="flex flex-wrap items-center gap-2" aria-hidden>
                    {WORKFLOW.map((w, i) => <span key={w} className="inline-flex items-center gap-1.5"><span className="h-1.5 w-1.5 animate-pulse-dot rounded-full bg-accent" style={{ animationDelay: `${i * 0.15}s` }} />{w}</span>)}
                  </span>
                </div>
              </div>
            </>
          )}
          <div ref={endRef} />
        </div>

        <form onSubmit={submit} className="sticky bottom-0 mt-6 bg-paper pt-2 pb-4">
          {send.isError && <div className="mb-2"><Notice tone="danger">{errorMessage(send.error)}</Notice></div>}
          <div className="flex items-end gap-2 rounded-2xl border border-line-strong bg-white p-2 shadow-card">
            <label htmlFor="chat-input" className="sr-only">Message the shopping assistant</label>
            <textarea
              id="chat-input"
              rows={1}
              className="max-h-40 min-h-11 flex-1 resize-none bg-transparent px-2 py-2.5 outline-none"
              placeholder={conversationId ? "Refine: “cheaper ones”, “add the second one to my cart”, “compare the top three”…" : "Describe what you need…"}
              value={input}
              maxLength={2000}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }}
            />
            <button className="btn btn-accent" type="submit" disabled={!input.trim() || send.isPending} aria-label="Send message"><Send size={16} aria-hidden /></button>
          </div>
          <p className="mt-1.5 text-center text-xs text-muted">Enter to send · Shift+Enter for a new line · Prices and stock come from the catalogue, never from the AI.</p>
        </form>
      </section>
    </div>
  );
}
