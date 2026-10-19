import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ListChecks } from "lucide-react";
import { type FormEvent, useRef, useState } from "react";
import { Link } from "react-router";
import { Container } from "../components/Layout";
import { EmptyState, ErrorState, PageHeader, Spinner } from "../components/ui";
import { useToast } from "../components/toastContext";
import { api } from "../lib/api";
import { formatDate, formatINR } from "../lib/format";
import { errorMessage, qk, useCategories } from "../lib/queries";
import { isProgressing, newIdempotencyKey, pollInterval, rerunInput, taskControls, TASK_POLL_MS } from "../lib/taskControls";
import type { AgentTask, ProductResearchInput, ProductResearchTask, TaskStatus, TaskStepStatus } from "../lib/types";

const STATUS_LABEL: Record<TaskStatus, string> = {
  queued: "Queued",
  running: "Running",
  succeeded: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
  awaiting_approval: "Awaiting approval",
};
const STATUS_CLS: Record<TaskStatus, string> = {
  queued: "border-line bg-paper-2",
  running: "border-accent/30 bg-accent-soft text-accent",
  succeeded: "border-leaf/25 bg-leaf-soft text-leaf",
  failed: "border-red-300 bg-red-50 text-red-700",
  cancelled: "border-line bg-paper-2 text-muted",
  awaiting_approval: "border-amber-300 bg-amber-50 text-amber-800",
};
const STEP_LABEL: Record<string, string> = {
  search: "Search the catalogue",
  compare: "Compare the top picks",
  cart: "Read your cart",
  summarize: "Summarise",
};
const STEP_ICON: Record<TaskStepStatus, string> = {
  queued: "○",
  running: "◐",
  succeeded: "●",
  failed: "✕",
  skipped: "–",
  cancelled: "⊘",
};

export function TasksPage() {
  const qc = useQueryClient();
  const tasks = useQuery({
    queryKey: qk.tasks,
    queryFn: api.tasks,
    // Poll only while a task is actually progressing (not interrupted or stalled ones).
    refetchInterval: (q) => pollInterval(q.state.data),
  });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: qk.tasks });
    qc.invalidateQueries({ queryKey: ["task"] });
  };
  // A newly created task appears at once (so polling starts at once), before the list refetches.
  const added = (created: AgentTask) => {
    qc.setQueryData<AgentTask[]>(qk.tasks, (old) => (old && !old.some((x) => x.id === created.id) ? [created, ...old] : old));
    refresh();
  };
  return (
    <Container className="max-w-4xl">
      <PageHeader eyebrow="Agent" title="Research tasks" />
      <p className="-mt-3 mb-6 text-sm text-muted">
        The agent searches, compares and summarises in the background, saving every step so an interrupted task
        can be resumed. Tasks only read: nothing is added to your cart or bought.
      </p>
      <NewTaskForm onCreated={added} />
      <section aria-labelledby="task-list" className="mt-8">
        <h2 id="task-list" className="mb-3 font-semibold">Your tasks</h2>
        {tasks.isLoading ? (
          <Spinner />
        ) : tasks.isError ? (
          <ErrorState error={tasks.error} onRetry={() => tasks.refetch()} />
        ) : !tasks.data?.length ? (
          <EmptyState title="No tasks yet" icon={ListChecks}>Start a research task above.</EmptyState>
        ) : (
          <ul className="space-y-4">
            {tasks.data.map((t) => (
              <TaskCard key={t.id} task={t} onChange={refresh} onCreated={added} />
            ))}
          </ul>
        )}
      </section>
    </Container>
  );
}

function NewTaskForm({ onCreated }: { onCreated: (task: AgentTask) => void }) {
  const toast = useToast();
  const categories = useCategories();
  const [category, setCategory] = useState("");
  const [query, setQuery] = useState("");
  const [maxPrice, setMaxPrice] = useState("");
  const [includeCart, setIncludeCart] = useState(true);
  const [formError, setFormError] = useState<string | null>(null);
  const inFlight = useRef(false);
  const create = useMutation({
    mutationFn: (input: ProductResearchInput) => api.createTask(input, newIdempotencyKey("research")),
    onSuccess: (task) => {
      toast("Research task started");
      onCreated(task);
    },
    onError: (e) => toast(errorMessage(e), "error"),
    onSettled: () => {
      inFlight.current = false;
    },
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    if (inFlight.current) return; // a second click before React re-renders the disabled button
    const q = query.trim();
    if (!category && !q) {
      setFormError("Choose a category or enter what you're looking for.");
      return;
    }
    const price = maxPrice.trim() ? Number(maxPrice) : undefined;
    if (price !== undefined && (!Number.isInteger(price) || price < 1)) {
      setFormError("Maximum price must be a whole number of rupees.");
      return;
    }
    setFormError(null);
    inFlight.current = true;
    create.mutate({
      ...(category ? { category } : {}),
      ...(q ? { query: q } : {}),
      ...(price !== undefined ? { max_price_inr: price } : {}),
      include_cart: includeCart,
    });
  }

  return (
    <form onSubmit={submit} className="card grid gap-4 p-5 sm:grid-cols-2" aria-label="New research task" noValidate>
      <label className="grid gap-1 text-sm">
        <span className="font-medium">Category</span>
        <select className="input" value={category} onChange={(e) => setCategory(e.target.value)}>
          <option value="">Any category</option>
          {categories.data?.map((c) => (
            <option key={c.key} value={c.key}>{c.label}</option>
          ))}
        </select>
      </label>
      <label className="grid gap-1 text-sm">
        <span className="font-medium">Looking for</span>
        <input className="input" value={query} maxLength={120} placeholder="e.g. noise cancelling" onChange={(e) => setQuery(e.target.value)} />
      </label>
      <label className="grid gap-1 text-sm">
        <span className="font-medium">Maximum price (₹)</span>
        <input className="input" inputMode="numeric" value={maxPrice} placeholder="e.g. 50000" onChange={(e) => setMaxPrice(e.target.value)} />
      </label>
      <label className="flex items-center gap-2 self-end text-sm">
        <input type="checkbox" checked={includeCart} onChange={(e) => setIncludeCart(e.target.checked)} />
        Include my cart in the summary
      </label>
      {formError && <p role="alert" className="text-sm text-red-700 sm:col-span-2">{formError}</p>}
      <div className="sm:col-span-2">
        <button type="submit" className="btn btn-primary" disabled={create.isPending}>
          {create.isPending ? "Starting…" : "Start research"}
        </button>
      </div>
    </form>
  );
}

function TaskCard({ task, onChange, onCreated }: { task: AgentTask; onChange: () => void; onCreated: (task: AgentTask) => void }) {
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const progressing = isProgressing(task);
  const controls = taskControls(task);
  const detail = useQuery({
    queryKey: qk.task(task.id),
    queryFn: () => api.task(task.id),
    enabled: open,
    refetchInterval: open && progressing ? TASK_POLL_MS : false,
  });
  // One action at a time per card. The ref blocks a second click that lands before React re-renders
  // the disabled buttons; the server still re-validates every action.
  const inFlight = useRef(false);
  // "Run again" keeps its key until it succeeds, so retrying after a network error can't create a duplicate.
  const rerunKey = useRef<string | null>(null);
  const failed = (e: unknown) => {
    toast(errorMessage(e), "error");
    onChange(); // e.g. 409: the task changed state elsewhere; show its current state
  };
  const settled = () => {
    inFlight.current = false;
  };
  const cancel = useMutation({
    mutationFn: () => api.cancelTask(task.id),
    onSuccess: (t) => {
      toast(t.status === "cancelled" ? "Task cancelled" : "Cancelling after the current step");
      onChange();
    },
    onError: failed,
    onSettled: settled,
  });
  const resume = useMutation({
    mutationFn: () => api.resumeTask(task.id),
    onSuccess: () => {
      toast("Task resumed");
      onChange();
    },
    onError: failed,
    onSettled: settled,
  });
  const rerun = useMutation({
    mutationFn: () => {
      if (task.kind !== "product_research") throw new Error("Only research tasks can be run again");
      rerunKey.current ??= newIdempotencyKey("rerun");
      return api.createTask(rerunInput(task.input), rerunKey.current);
    },
    onSuccess: (created) => {
      rerunKey.current = null;
      toast("Running the task again");
      onCreated(created);
    },
    onError: (e) => toast(errorMessage(e), "error"),
    onSettled: settled,
  });
  const busy = cancel.isPending || resume.isPending || rerun.isPending;
  const act = (m: { mutate: () => void }) => () => {
    if (inFlight.current) return;
    inFlight.current = true;
    m.mutate();
  };
  const shown = detail.data ?? task;
  const statusText = task.interrupted ? "Interrupted" : task.stalled ? "Stalled" : STATUS_LABEL[task.status];

  return (
    <li className="card p-5" data-testid="task-card">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-semibold">{task.goal}</p>
          <p className="text-sm text-muted">Started {formatDate(task.created_at)}</p>
        </div>
        <span className={`chip ${STATUS_CLS[task.status]}`} aria-live="polite" data-testid="task-status">
          {statusText}
          {task.cancel_requested && task.status === "running" ? " · cancelling" : ""}
        </span>
      </div>
      <div
        className="mt-3 h-2 overflow-hidden rounded-full bg-paper-2"
        role="progressbar"
        aria-label={`Progress: ${task.goal}`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={task.progress}
      >
        <div className="h-full bg-accent transition-all" style={{ width: `${task.progress}%` }} />
      </div>
      <p className="mt-1 text-xs text-muted">
        Step {Math.min(task.current_step + (progressing ? 1 : 0), task.total_steps)} of {task.total_steps} · {task.progress}%
      </p>

      {task.error && (
        <p className="mt-3 text-sm text-red-700" role="status">
          {task.error.message}
          {task.error.retryable ? " — this is temporary; you can resume." : ""}
        </p>
      )}
      {task.kind === "product_research" && task.result && <ResearchSummary task={task} />}

      <div className="mt-4 flex flex-wrap gap-2">
        <button type="button" className="btn btn-sm" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
          {open ? "Hide steps" : "Show steps"}
        </button>
        {controls.cancel && (
          <button type="button" className="btn btn-sm" onClick={act(cancel)} disabled={busy} aria-busy={cancel.isPending}>
            {cancel.isPending ? "Cancelling…" : "Cancel"}
          </button>
        )}
        {controls.resume && (
          <button type="button" className="btn btn-primary btn-sm" onClick={act(resume)} disabled={busy} aria-busy={resume.isPending}>
            {resume.isPending ? "Resuming…" : "Resume"}
          </button>
        )}
        {controls.runAgain && (
          <button type="button" className="btn btn-sm" onClick={act(rerun)} disabled={busy} aria-busy={rerun.isPending}>
            {rerun.isPending ? "Starting…" : "Run again"}
          </button>
        )}
      </div>

      {open && (
        <ol className="mt-4 space-y-2 border-t border-line pt-3 text-sm" aria-label="Task steps">
          {detail.isLoading && <Spinner />}
          {shown.steps?.map((s) => (
            <li key={s.position} className="flex flex-wrap items-baseline justify-between gap-2" data-testid="task-step">
              <span>
                <span aria-hidden className="mr-2">{STEP_ICON[s.status]}</span>
                {STEP_LABEL[s.name] ?? s.name}
                <span className="sr-only"> — {s.status}</span>
              </span>
              <span className="text-xs text-muted">
                {s.status}
                {s.attempts > 1 ? ` · ${s.attempts} attempts` : ""}
                {s.status === "skipped" && s.output && typeof s.output.reason === "string" ? ` · ${s.output.reason}` : ""}
              </span>
            </li>
          ))}
        </ol>
      )}
    </li>
  );
}

function ResearchSummary({ task }: { task: ProductResearchTask }) {
  const r = task.result;
  if (!r) return null;
  return (
    <div className="mt-4 rounded-lg border border-line p-3 text-sm" data-testid="task-result">
      <p>
        {r.matches} matching product{r.matches === 1 ? "" : "s"}
        {task.input.max_price_inr ? ` under ${formatINR(task.input.max_price_inr)}` : ""}.
        {r.cart ? ` Your cart: ${r.cart.item_count} item${r.cart.item_count === 1 ? "" : "s"}, ${formatINR(r.cart.total_inr)}.` : ""}
      </p>
      {r.top.length > 0 && (
        <ul className="mt-2 divide-y divide-line">
          {r.top.map((p) => (
            <li key={p.id} className="flex justify-between gap-3 py-1.5">
              <Link to={`/products/${p.id}`} className="hover:text-accent">{p.name}</Link>
              <span className="tabular-nums text-muted">{formatINR(p.price_inr)} · {p.rating.toFixed(1)}★</span>
            </li>
          ))}
        </ul>
      )}
      {r.best.price_inr?.length ? <p className="mt-2 text-muted">Lowest price: {r.best.price_inr.join(", ")}</p> : null}
      {r.best.rating?.length ? <p className="text-muted">Best rated: {r.best.rating.join(", ")}</p> : null}
    </div>
  );
}
