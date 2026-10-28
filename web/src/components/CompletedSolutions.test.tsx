// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ProjectWorkItem,
  ProjectWorkListResponse,
  ProjectWorkStatus,
} from "@/lib/api";
import { CompletedSolutionsTable } from "./CompletedSolutions";

const apiMocks = vi.hoisted(() => ({
  listProjectWorkItems: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ api: apiMocks }));

const timestamp = "2026-08-20T02:00:00Z";

function workItem(index: number): ProjectWorkItem {
  return {
    work_item_id: `work-${index}`,
    candidate_id: `candidate-${index}`,
    repository_id: 101,
    repository: "acme/alpha",
    issue_number: index,
    issue_url: `https://github.com/acme/alpha/issues/${index}`,
    title: `Fix runtime regression ${index}`,
    status: "done",
    current_step: "Both review stages approved the solution.",
    plan: null,
    environment_status: "verified",
    environment_verified_at: timestamp,
    named_baseline: null,
    resource_requirements: null,
    task_id: null,
    run_id: null,
    execution_id: null,
    internal_candidate_id: `internal-${index}`,
    blocked_reason: null,
    last_error: null,
    execution_attempt: 1,
    retry_not_before: null,
    queued_at: timestamp,
    planning_started_at: timestamp,
    started_at: timestamp,
    review_started_at: timestamp,
    completed_at: timestamp,
    updated_at: timestamp,
  };
}

function listResponse(
  items: ProjectWorkItem[],
  done: number,
  offset = 0,
): ProjectWorkListResponse {
  const counts: Record<ProjectWorkStatus, number> = {
    queued: 0,
    planning: 0,
    running: 0,
    review: 0,
    done,
    blocked: 0,
    failed: 0,
  };
  return { work_items: items, counts, total: done, limit: 25, offset };
}

async function settle() {
  await act(async () => {
    for (let tick = 0; tick < 5; tick += 1) await Promise.resolve();
  });
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.clearAllMocks();
});

describe("CompletedSolutionsTable", () => {
  it("renders every completed Work item returned by the backend", async () => {
    const items = [workItem(1), workItem(2)];
    apiMocks.listProjectWorkItems.mockResolvedValue(listResponse(items, 2));

    await act(async () => root.render(<CompletedSolutionsTable />));
    await settle();

    expect(apiMocks.listProjectWorkItems).toHaveBeenCalledWith({
      status: ["done"],
      limit: 25,
      offset: 0,
    });
    expect(container.textContent).toContain("Completed solutions · 2");
    expect(container.querySelectorAll("[data-completed-solution]")).toHaveLength(2);
    for (const item of items) {
      const row = container.querySelector(
        `[data-completed-solution="${item.work_item_id}"]`,
      );
      const link = row?.querySelector<HTMLAnchorElement>(
        `a[href="${item.issue_url}"]`,
      );
      expect(link?.textContent).toContain(`Issue #${item.issue_number}`);
      expect(row?.textContent).toContain(item.title);
      expect(row?.textContent).toContain(item.internal_candidate_id);
    }
  });

  it("shows the empty state when no Work has completed", async () => {
    apiMocks.listProjectWorkItems.mockResolvedValue(listResponse([], 0));

    await act(async () => root.render(<CompletedSolutionsTable />));
    await settle();

    expect(container.querySelectorAll("[data-completed-solution]")).toHaveLength(0);
    expect(container.textContent).toContain("No completed Work has been recorded yet.");
  });

  it("requests the next page of completed Work", async () => {
    apiMocks.listProjectWorkItems.mockImplementation(
      async ({ offset }: { offset: number }) =>
        listResponse([workItem(offset + 1)], 30, offset),
    );

    await act(async () => root.render(<CompletedSolutionsTable />));
    await settle();
    expect(container.textContent).toContain("1–25 of 30 · page 1 / 2");

    const next = container.querySelector<HTMLButtonElement>(
      'button[aria-label="Next completed solutions page"]',
    );
    await act(async () => {
      next?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await settle();

    expect(apiMocks.listProjectWorkItems).toHaveBeenLastCalledWith({
      status: ["done"],
      limit: 25,
      offset: 25,
    });
    expect(container.textContent).toContain("26–30 of 30 · page 2 / 2");
    expect(
      container.querySelector('[data-completed-solution="work-26"]'),
    ).not.toBeNull();
  });

  it("surfaces a backend failure", async () => {
    apiMocks.listProjectWorkItems.mockRejectedValue(
      new Error("work-items endpoint unavailable"),
    );

    await act(async () => root.render(<CompletedSolutionsTable />));
    await settle();

    expect(container.querySelector('[role="alert"]')?.textContent).toContain(
      "work-items endpoint unavailable",
    );
  });

  it("selects a completed Work item when its row is clicked", async () => {
    const item = workItem(7);
    const onSelect = vi.fn();
    apiMocks.listProjectWorkItems.mockResolvedValue(listResponse([item], 1));

    await act(async () => root.render(<CompletedSolutionsTable onSelect={onSelect} />));
    await settle();

    const row = container.querySelector<HTMLTableRowElement>(
      '[data-completed-solution="work-7"]',
    );
    await act(async () => {
      row?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(onSelect).toHaveBeenCalledWith(item);
  });
});
