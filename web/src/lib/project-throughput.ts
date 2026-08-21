import type { ProjectIssueFeedItem } from "@/lib/api";

export interface DailyThroughputPoint {
  date: string;
  collected: number;
  selected: number;
  queued: number;
  resolved: number;
}

interface ThroughputOptions {
  days?: number;
  now?: Date;
}

function utcDateKey(value: string | Date): string | null {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toISOString().slice(0, 10);
}

function increment(
  pointsByDate: Map<string, DailyThroughputPoint>,
  value: string | null,
  metric: Exclude<keyof DailyThroughputPoint, "date">,
) {
  if (!value) return;
  const key = utcDateKey(value);
  const point = key ? pointsByDate.get(key) : undefined;
  if (point) point[metric] += 1;
}

export function buildDailyThroughput(
  items: readonly ProjectIssueFeedItem[],
  { days = 14, now = new Date() }: ThroughputOptions = {},
): DailyThroughputPoint[] {
  if (!Number.isInteger(days) || days < 1) {
    throw new RangeError("days must be a positive integer");
  }

  const end = Date.UTC(
    now.getUTCFullYear(),
    now.getUTCMonth(),
    now.getUTCDate(),
  );
  const points = Array.from({ length: days }, (_, index) => {
    const offset = index - days + 1;
    const date = new Date(end + offset * 86_400_000).toISOString().slice(0, 10);
    return { date, collected: 0, selected: 0, queued: 0, resolved: 0 };
  });
  const pointsByDate = new Map(points.map((point) => [point.date, point]));
  const collectedCandidates = new Set<string>();
  const selectedCandidates = new Set<string>();
  const queuedWorkItems = new Set<string>();
  const resolvedWorkItems = new Set<string>();

  for (const item of items) {
    const { candidate, screening, work_item: workItem } = item;

    if (!collectedCandidates.has(candidate.candidate_id)) {
      collectedCandidates.add(candidate.candidate_id);
      increment(pointsByDate, candidate.first_seen_at, "collected");
    }

    if (
      screening?.decision === "SELECT" &&
      !selectedCandidates.has(candidate.candidate_id)
    ) {
      selectedCandidates.add(candidate.candidate_id);
      increment(pointsByDate, screening.screened_at, "selected");
    }

    if (workItem && !queuedWorkItems.has(workItem.work_item_id)) {
      queuedWorkItems.add(workItem.work_item_id);
      increment(pointsByDate, workItem.queued_at, "queued");
    }

    if (
      workItem?.status === "done" &&
      !resolvedWorkItems.has(workItem.work_item_id)
    ) {
      resolvedWorkItems.add(workItem.work_item_id);
      increment(pointsByDate, workItem.completed_at, "resolved");
    }
  }

  return points;
}

