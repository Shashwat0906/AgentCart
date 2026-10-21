import type { AgentTask, ProductResearchInput, TaskStatus } from "./types";

export const TASK_POLL_MS = 1500;

type TaskState = Pick<AgentTask, "kind" | "status" | "interrupted" | "stalled" | "resumable" | "cancel_requested">;

/** Something is actually progressing on the server, so its status can change by itself. Interrupted
 *  and stalled tasks won't change until the user resumes or cancels them, so they are not polled. */
export function isProgressing(t: Pick<AgentTask, "status" | "interrupted" | "stalled">): boolean {
  return (t.status === "queued" && !t.stalled) || (t.status === "running" && !t.interrupted);
}

export function pollInterval(tasks: readonly AgentTask[] | undefined): number | false {
  return tasks?.some(isProgressing) ? TASK_POLL_MS : false;
}

const FINISHED: readonly TaskStatus[] = ["succeeded", "failed", "cancelled"];

export interface TaskControls {
  cancel: boolean;
  resume: boolean;
  runAgain: boolean;
}

/** Which actions a task card offers. The server stays the authority: it re-checks every action. */
export function taskControls(t: TaskState): TaskControls {
  const pending = t.status === "queued" || t.status === "running";
  return {
    cancel: pending && !t.cancel_requested,
    resume: t.resumable && !t.cancel_requested,
    // Run again re-sends a research form; agent tasks get their own controls in a later stage.
    runAgain: t.kind === "product_research" && FINISHED.includes(t.status),
  };
}

const INPUT_KEYS = ["query", "category", "max_price_inr", "min_rating", "in_stock", "compare_top", "include_cart"] as const;

/** The task's own input, limited to the fields the API accepts, without empty values. */
export function rerunInput(input: ProductResearchInput): ProductResearchInput {
  const out: Record<string, unknown> = {};
  for (const k of INPUT_KEYS) {
    const v = input[k];
    if (v !== null && v !== undefined) out[k] = v;
  }
  return out as ProductResearchInput;
}

export function newIdempotencyKey(prefix: "research" | "rerun"): string {
  const id = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
  return `${prefix}-${id}`;
}
