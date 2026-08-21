// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { InternalPullRequestCandidate } from "@/lib/api";
import ChangesPage from "./ChangesPage";

const apiMocks = vi.hoisted(() => ({
  getInternalPullRequestCandidate: vi.fn(),
  getInternalPullRequestCandidateFileDiff: vi.fn(),
  listInternalPullRequestCandidates: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ api: apiMocks }));
vi.mock("@/contexts/usePageHeader", () => ({
  usePageHeader: () => ({ setEnd: vi.fn() }),
}));
vi.mock("@/components/Markdown", () => ({
  Markdown: ({ content }: { content: string }) => <div>{content}</div>,
}));

const candidate: InternalPullRequestCandidate = {
  schema_version: "internal-pr-candidate.v1",
  candidate_id: "candidate-17",
  task_id: "task-17",
  title: "Fix the kernel boundary",
  body: "Candidate body",
  repository: "acme/kernel",
  base_ref: "main",
  base_sha: "a".repeat(40),
  head_ref: "project-hermes/issue-17",
  head_sha: "b".repeat(40),
  commits: [
    {
      sha: "b".repeat(40),
      message: "Fix the kernel boundary",
      author_name: "ProjectHermes",
      author_email: null,
      authored_at: "2026-08-13T08:00:00Z",
    },
  ],
  files: [
    {
      path: "src/kernel.py",
      status: "M",
      added: 1,
      removed: 1,
      is_binary: false,
      production_scope: true,
      necessity: "Required production fix.",
    },
  ],
  checks: [],
  reviews: [],
  accounting: {
    wall_clock_duration_ms: 0,
    active_duration_ms: 0,
    estimated_llm_cost_usd: 0,
    cost_complete: false,
    rounds: [],
  },
  lock_state: "approval_pending",
  lock_digest: null,
  locked_by: null,
  locked_at: null,
  post_lock_comparison: {
    summary: "SECRET REFERENCE SUMMARY",
    defects: ["SECRET DEFECT"],
    coverage: [],
    minimality: [],
    compared_at: "2026-08-13T08:00:00Z",
  },
  created_at: "2026-08-13T08:00:00Z",
  updated_at: "2026-08-13T08:00:00Z",
};

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  apiMocks.listInternalPullRequestCandidates.mockReset();
  apiMocks.getInternalPullRequestCandidate.mockReset();
  apiMocks.getInternalPullRequestCandidateFileDiff.mockReset();
  apiMocks.listInternalPullRequestCandidates.mockResolvedValue({
    candidates: [],
    total: 0,
    limit: 100,
    offset: 0,
  });
  apiMocks.getInternalPullRequestCandidate.mockResolvedValue(candidate);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

describe("ChangesPage internal candidates", () => {
  it("withholds comparison content until immutable approval", async () => {
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/changes?candidate=candidate-17"]}>
          <ChangesPage />
        </MemoryRouter>,
      );
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const comparison = Array.from(container.querySelectorAll("button")).find(
      (button) => button.textContent === "Comparison",
    );
    expect(comparison).toBeDefined();
    await act(async () => {
      comparison?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(container.textContent).toContain("reference PR withheld");
    expect(container.textContent).not.toContain("SECRET REFERENCE SUMMARY");
    expect(container.textContent).not.toContain("SECRET DEFECT");
    for (const label of [
      "Conversation",
      "Commits",
      "Files changed",
      "Checks",
      "Reviews",
      "Comparison",
      "Accounting",
    ]) {
      expect(container.textContent).toContain(label);
    }
  });
});
