import { describe, expect, it } from "vitest";

import type { ProjectIssueFeedItem, ProjectWorkStatus } from "@/lib/api";
import { buildDailyThroughput } from "./project-throughput";

function issue(
  id: string,
  values: {
    firstSeen: string;
    screenedAt?: string;
    queuedAt?: string;
    completedAt?: string | null;
    status?: ProjectWorkStatus;
  },
): ProjectIssueFeedItem {
  return {
    candidate: {
      candidate_id: id,
      repository_id: 1,
      repository: "acme/widgets",
      issue_number: Number(id.replace(/\D/g, "")) || 1,
      issue_url: `https://github.com/acme/widgets/issues/${id}`,
      title: `Issue ${id}`,
      body: "",
      labels: [],
      author: null,
      comments: 0,
      evidence_score: 1,
      eligible: true,
      filter_reasons: [],
      created_at: values.firstSeen,
      updated_at: values.firstSeen,
      first_seen_at: values.firstSeen,
      last_seen_at: values.firstSeen,
      latest_run_id: "run-1",
    },
    screening: values.screenedAt
      ? {
          candidate_id: id,
          candidate_snapshot_digest: "digest",
          decision: "SELECT",
          machine_compatibility: "COMPATIBLE",
          task_kind: "LOCAL_CODE_OR_TEST",
          reason: "Selected",
          required_environment: {
            operating_systems: [],
            cpu_architectures: [],
            minimum_cpu_cores: 1,
            minimum_memory_gib: 1,
            gpu_count: 0,
            gpu_architectures: [],
            network_access_required: false,
            external_system_write_required: false,
            external_dependencies: [],
          },
          evidence: [],
          uncertainties: [],
          model_provider: "test",
          model: "test",
          session_id: "session-1",
          profile_digest: "profile",
          soul_digest: "soul",
          agent_digest: "agent",
          revision: 1,
          screening_trigger: "initial",
          requested_by: null,
          request_reason: null,
          probe_evidence: [],
          screened_at: values.screenedAt,
        }
      : null,
    work_item: values.queuedAt
      ? {
          work_item_id: `work-${id}`,
          candidate_id: id,
          repository_id: 1,
          repository: "acme/widgets",
          issue_number: 1,
          issue_url: "https://github.com/acme/widgets/issues/1",
          title: `Issue ${id}`,
          status: values.status ?? "queued",
          current_step: "",
          plan: null,
          environment_status: "pending",
          environment_verified_at: null,
          named_baseline: null,
          resource_requirements: null,
          task_id: null,
          run_id: null,
          execution_id: null,
          internal_candidate_id: null,
          blocked_reason: null,
          last_error: null,
          execution_attempt: 0,
          retry_not_before: null,
          queued_at: values.queuedAt,
          planning_started_at: null,
          started_at: null,
          review_started_at: null,
          completed_at: values.completedAt ?? null,
          updated_at: values.completedAt ?? values.queuedAt,
        }
      : null,
  };
}

describe("buildDailyThroughput", () => {
  it("groups intake and delivery events into zero-filled UTC days", () => {
    const result = buildDailyThroughput(
      [
        issue("17", {
          firstSeen: "2026-08-19T23:59:00Z",
          screenedAt: "2026-08-20T00:01:00Z",
          queuedAt: "2026-08-20T02:00:00Z",
          completedAt: "2026-08-21T03:00:00Z",
          status: "done",
        }),
        issue("18", {
          firstSeen: "2026-08-21T04:00:00Z",
          queuedAt: "2026-08-21T05:00:00Z",
          completedAt: "2026-08-21T06:00:00Z",
          status: "failed",
        }),
      ],
      { days: 3, now: new Date("2026-08-21T12:00:00Z") },
    );

    expect(result).toEqual([
      { date: "2026-08-19", collected: 1, selected: 0, queued: 0, resolved: 0 },
      { date: "2026-08-20", collected: 0, selected: 1, queued: 1, resolved: 0 },
      { date: "2026-08-21", collected: 1, selected: 0, queued: 1, resolved: 1 },
    ]);
  });

  it("deduplicates repeated feed rows and ignores events outside the window", () => {
    const repeated = issue("17", {
      firstSeen: "2026-08-21T01:00:00Z",
      screenedAt: "2026-08-21T02:00:00Z",
      queuedAt: "2026-08-21T03:00:00Z",
    });
    const result = buildDailyThroughput(
      [
        repeated,
        repeated,
        issue("old", { firstSeen: "2026-07-01T00:00:00Z" }),
      ],
      { days: 1, now: new Date("2026-08-21T12:00:00Z") },
    );

    expect(result[0]).toEqual({
      date: "2026-08-21",
      collected: 1,
      selected: 1,
      queued: 1,
      resolved: 0,
    });
  });

  it("rejects invalid window lengths", () => {
    expect(() => buildDailyThroughput([], { days: 0 })).toThrow(RangeError);
  });
});
