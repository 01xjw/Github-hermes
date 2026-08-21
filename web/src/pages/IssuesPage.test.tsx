// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  PollingOverviewResponse,
  ProjectIssueCandidate,
  ProjectIssueDetailResponse,
  ProjectIssueScreening,
} from "@/lib/api";
import IssuesPage from "./IssuesPage";

const apiMocks = vi.hoisted(() => ({
  getPollingOverview: vi.fn(),
  getProjectIssue: vi.fn(),
  listProjectIssues: vi.fn(),
  rescreenProjectIssue: vi.fn(),
  selectProjectIssue: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ api: apiMocks }));
vi.mock("@/contexts/usePageHeader", () => ({
  usePageHeader: () => ({ setEnd: vi.fn() }),
}));

const timestamp = "2026-08-20T02:00:00Z";
const candidate: ProjectIssueCandidate = {
  candidate_id: "candidate-17",
  repository_id: 101,
  repository: "acme/alpha",
  issue_number: 17,
  issue_url: "https://github.com/acme/alpha/issues/17",
  title: "Compatibility namespace needs a verified re-screen",
  body: "A CPU tensor reproducer is included.",
  labels: ["module: runtime"],
  author: "reporter",
  comments: 2,
  evidence_score: 5,
  eligible: true,
  filter_reasons: [],
  created_at: timestamp,
  updated_at: timestamp,
  first_seen_at: timestamp,
  last_seen_at: timestamp,
  latest_run_id: "poll-17",
};

function screening(
  revision: number,
  decision: "DEFER" | "SELECT",
): ProjectIssueScreening {
  const rescreened = revision > 1;
  return {
    candidate_id: candidate.candidate_id,
    candidate_snapshot_digest: "a".repeat(64),
    decision,
    machine_compatibility: decision === "SELECT" ? "COMPATIBLE" : "NEEDS_PROBE",
    task_kind: "LOCAL_REPRODUCTION",
    reason: decision === "SELECT"
      ? "The production-worker probe proved the compatibility alias."
      : "The compatibility alias needs one bounded worker probe.",
    required_environment: {
      operating_systems: ["linux"],
      cpu_architectures: ["amd64"],
      minimum_cpu_cores: 1,
      minimum_memory_gib: 4,
      gpu_count: 0,
      gpu_architectures: [],
      network_access_required: false,
      external_system_write_required: false,
      external_dependencies: decision === "SELECT" ? [] : ["runtime evidence"],
    },
    evidence: ["The Issue contains a CPU tensor reproducer."],
    uncertainties: decision === "SELECT" ? [] : ["Run it in the worker image."],
    model_provider: "deepseek",
    model: "deepseek-chat",
    session_id: `screening-${revision}`,
    profile_digest: "b".repeat(64),
    soul_digest: "c".repeat(64),
    agent_digest: "d".repeat(64),
    revision,
    screening_trigger: rescreened ? "operator_rescreen" : "initial",
    requested_by: rescreened ? "operator-session" : null,
    request_reason: rescreened ? "The bounded worker probe completed." : null,
    probe_evidence: rescreened ? ["The CPU reproducer passed."] : [],
    screened_at: timestamp,
  };
}

const initial = screening(1, "DEFER");
const initialDetail: ProjectIssueDetailResponse = {
  candidate,
  work_item: null,
  screening: initial,
  screening_history: [initial],
  events: [],
  operator_selection_required: true,
  screening_selection_required: true,
  operator_selected: false,
  operator_selectable: false,
};
const current = screening(2, "SELECT");
const rescreenedDetail: ProjectIssueDetailResponse = {
  ...initialDetail,
  screening: current,
  screening_history: [initial, current],
  operator_selectable: true,
};

const overview: PollingOverviewResponse = {
  task: {
    task_id: "github-issue-polling",
    enabled: true,
    interval_seconds: 900,
    status: "idle",
    next_run_at: null,
    last_run_id: null,
    last_started_at: null,
    last_completed_at: null,
    last_error: null,
    created_at: timestamp,
    updated_at: timestamp,
  },
  latest_run: null,
  selected_repository_id: null,
  repository_count: 1,
  repositories_in_view: 1,
  issues_seen: 1,
  polling_run_count: 1,
  candidate_counts: { total: 1, matched: 1, filtered: 0 },
  screening_counts: { SELECT: 0, DEFER: 1, REJECT: 0, PENDING: 0 },
  work_counts: { queued: 0, planning: 0, running: 0, review: 0, done: 0, blocked: 0, failed: 0 },
  processed: { today: 0, this_week: 0 },
  repository_stats: [],
  manager: null,
  supervisor: null,
  operator_selection_required: true,
  screening_selection_required: true,
};

let container: HTMLDivElement;
let root: Root;

async function flushEffects() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function enterText(element: HTMLTextAreaElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,
    "value",
  )?.set;
  await act(async () => {
    setter?.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

beforeEach(() => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  for (const mock of Object.values(apiMocks)) mock.mockReset();
  apiMocks.getPollingOverview.mockResolvedValue(overview);
  apiMocks.listProjectIssues.mockResolvedValue({
    issues: [{ candidate, screening: initial, work_item: null }],
    total: 1,
    limit: 50,
    offset: 0,
  });
  apiMocks.getProjectIssue.mockResolvedValue(initialDetail);
  apiMocks.rescreenProjectIssue.mockResolvedValue(rescreenedDetail);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

describe("IssuesPage screening revisions", () => {
  it("submits bounded probe evidence without approving Work", async () => {
    await act(async () => root.render(<IssuesPage />));
    await flushEffects();

    expect(container.textContent).toContain("revision 1");
    expect(container.textContent).toContain("Screening history · 1 immutable revision");
    expect(container.textContent).toContain("it does not approve the Issue for Work");

    const textareas = container.querySelectorAll("textarea");
    expect(textareas).toHaveLength(2);
    await enterText(
      textareas[0],
      "  The production AMD worker resolved the API alias question.  ",
    );
    await enterText(
      textareas[1],
      " first observed fact \n\n second observed fact ",
    );
    const button = Array.from(container.querySelectorAll("button")).find(
      (item) => item.textContent?.includes("Run screening Subagent again"),
    );
    expect(button?.disabled).toBe(false);
    await act(async () => {
      button?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      await Promise.resolve();
    });

    expect(apiMocks.rescreenProjectIssue).toHaveBeenCalledWith("candidate-17", {
      reason: "The production AMD worker resolved the API alias question.",
      probe_evidence: ["first observed fact", "second observed fact"],
    });
    expect(apiMocks.selectProjectIssue).not.toHaveBeenCalled();
  });
});
