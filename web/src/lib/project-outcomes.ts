import type {
  PollingOverviewResponse,
  ProjectWorkStatus,
} from "@/lib/api";

export const PROJECT_WORK_STATUS_ORDER = [
  "queued",
  "planning",
  "running",
  "review",
  "done",
  "blocked",
  "failed",
] as const satisfies readonly ProjectWorkStatus[];

export interface ProjectOutcomeSummary {
  screened: number;
  selected: number;
  workItems: number;
  active: number;
  terminal: number;
  delivered: number;
  needsAttention: number;
  selectionRate: number;
  terminalSuccessRate: number;
}

function percentage(numerator: number, denominator: number): number {
  if (denominator <= 0) return 0;
  return Math.round((numerator / denominator) * 1000) / 10;
}

export function buildProjectOutcomeSummary(
  overview: Pick<
    PollingOverviewResponse,
    "screening_counts" | "work_counts"
  >,
): ProjectOutcomeSummary {
  const screened =
    overview.screening_counts.SELECT
    + overview.screening_counts.DEFER
    + overview.screening_counts.REJECT;
  const workItems = PROJECT_WORK_STATUS_ORDER.reduce(
    (total, status) => total + overview.work_counts[status],
    0,
  );
  const active =
    overview.work_counts.queued
    + overview.work_counts.planning
    + overview.work_counts.running
    + overview.work_counts.review;
  const terminal =
    overview.work_counts.done
    + overview.work_counts.blocked
    + overview.work_counts.failed;

  return {
    screened,
    selected: overview.screening_counts.SELECT,
    workItems,
    active,
    terminal,
    delivered: overview.work_counts.done,
    needsAttention:
      overview.work_counts.blocked + overview.work_counts.failed,
    selectionRate: percentage(overview.screening_counts.SELECT, screened),
    terminalSuccessRate: percentage(overview.work_counts.done, terminal),
  };
}
