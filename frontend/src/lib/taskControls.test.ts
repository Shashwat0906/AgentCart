import { describe, expect, it } from "vitest";
import { isProgressing, newIdempotencyKey, pollInterval, rerunInput, taskControls, TASK_POLL_MS } from "./taskControls";
import type { ProductResearchTask, TaskStatus } from "./types";

function t(status: TaskStatus, over: Partial<ProductResearchTask> = {}): ProductResearchTask {
  return {
    id: status, kind: "product_research", goal: "g", status, progress: 0, current_step: 0, total_steps: 4,
    input: { category: "laptops" }, result: null, error: null, cancel_requested: false, attempt: 0,
    interrupted: false, stalled: false, resumable: false, created_at: "", updated_at: "", started_at: null,
    finished_at: null, steps: null, ...over,
  };
}

describe("taskControls", () => {
  it.each([
    ["queued (fresh)", t("queued"), { cancel: true, resume: false, runAgain: false }],
    ["queued (stalled)", t("queued", { stalled: true, resumable: true }), { cancel: true, resume: true, runAgain: false }],
    ["running", t("running"), { cancel: true, resume: false, runAgain: false }],
    ["running (cancelling)", t("running", { cancel_requested: true }), { cancel: false, resume: false, runAgain: false }],
    ["interrupted", t("running", { interrupted: true, resumable: true }), { cancel: true, resume: true, runAgain: false }],
    ["succeeded", t("succeeded"), { cancel: false, resume: false, runAgain: true }],
    ["failed (temporary)", t("failed", { resumable: true }), { cancel: false, resume: true, runAgain: true }],
    ["failed (permanent)", t("failed"), { cancel: false, resume: false, runAgain: true }],
    ["cancelled", t("cancelled"), { cancel: false, resume: false, runAgain: true }],
  ])("%s", (_label, task, expected) => {
    expect(taskControls(task)).toEqual(expected);
  });

  it("never offers Run again for agent tasks (it re-sends a research form)", () => {
    for (const status of ["succeeded", "failed", "cancelled"] as const) {
      expect(taskControls({ ...t(status), kind: "shopping_agent" }).runAgain).toBe(false);
    }
  });
});

describe("polling", () => {
  it("polls while a task is queued or running", () => {
    expect(pollInterval([t("succeeded"), t("queued")])).toBe(TASK_POLL_MS);
    expect(pollInterval([t("running")])).toBe(TASK_POLL_MS);
  });
  it("stops for finished, interrupted and stalled tasks, which can't change on their own", () => {
    expect(pollInterval([t("succeeded"), t("failed"), t("cancelled")])).toBe(false);
    expect(pollInterval([t("running", { interrupted: true })])).toBe(false);
    expect(pollInterval([t("queued", { stalled: true })])).toBe(false);
    expect(pollInterval([])).toBe(false);
    expect(pollInterval(undefined)).toBe(false);
    expect(isProgressing(t("queued", { stalled: true }))).toBe(false);
  });
});

describe("rerunInput", () => {
  it("keeps only accepted, non-empty fields", () => {
    const stored = { query: null, category: "laptops", max_price_inr: 50000, min_rating: null, in_stock: true, compare_top: 3, include_cart: false, extra: "x" };
    expect(rerunInput(stored as never)).toEqual({ category: "laptops", max_price_inr: 50000, in_stock: true, compare_top: 3, include_cart: false });
  });
});

describe("newIdempotencyKey", () => {
  it("matches the server's key format and is unique", () => {
    const a = newIdempotencyKey("rerun");
    expect(a).toMatch(/^rerun-[A-Za-z0-9._:-]+$/);
    expect(a.length).toBeGreaterThanOrEqual(8);
    expect(a.length).toBeLessThanOrEqual(80);
    expect(newIdempotencyKey("rerun")).not.toBe(a);
  });
});
