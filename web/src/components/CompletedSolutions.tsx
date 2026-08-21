import { useCallback, useEffect, useRef, useState } from "react";
import {
  CheckCircle2,
  ExternalLink,
  GitPullRequest,
  RefreshCw,
} from "lucide-react";

import { ProjectPanel } from "@/components/ProjectHermesUi";
import {
  COMPLETED_SOLUTIONS,
  type CompletedSolution,
  type UpstreamPullRequestState,
} from "@/lib/completed-solutions";
import { api, type ProjectPullRequestStatus } from "@/lib/api";
import { cn } from "@/lib/utils";

const UPSTREAM_TONES = {
  open: "border-blue-200 bg-blue-50 text-blue-700",
  draft: "border-amber-200 bg-amber-50 text-amber-700",
  merged: "border-violet-200 bg-violet-50 text-violet-700",
  closed: "border-slate-200 bg-slate-100 text-slate-600",
};
const GITHUB_REFRESH_INTERVAL_MS = 5 * 60_000;

interface LivePullRequestState {
  state: UpstreamPullRequestState;
  checkedAt: string | null;
  stale: boolean;
}

function initialPullRequestStates(): Record<string, LivePullRequestState> {
  return Object.fromEntries(
    COMPLETED_SOLUTIONS.map((solution) => [
      solution.id,
      { state: solution.upstreamState, checkedAt: null, stale: true },
    ]),
  );
}

function useLivePullRequestStates() {
  const [states, setStates] = useState(initialPullRequestStates);
  const [refreshing, setRefreshing] = useState(false);
  const inFlight = useRef(false);
  const mounted = useRef(true);

  const refresh = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setRefreshing(true);
    try {
      const response = await api.getProjectPullRequestStatuses(
        COMPLETED_SOLUTIONS.map((solution) => ({
          key: solution.id,
          repository: solution.repository,
          number: solution.pullRequestNumber,
        })),
      );
      if (!mounted.current) return;
      const results = new Map(
        response.statuses.map((status) => [status.key, status]),
      );
      setStates((current) => {
        const next = { ...current };
        COMPLETED_SOLUTIONS.forEach((solution) => {
          const result: ProjectPullRequestStatus | undefined = results.get(
            solution.id,
          );
          if (result && result.state !== "unknown") {
            next[solution.id] = {
              state: result.state,
              checkedAt: result.checked_at,
              stale: false,
            };
          } else {
            next[solution.id] = { ...next[solution.id], stale: true };
          }
        });
        return next;
      });
    } catch {
      if (mounted.current) {
        setStates((current) =>
          Object.fromEntries(
            Object.entries(current).map(([key, status]) => [
              key,
              { ...status, stale: true },
            ]),
          ),
        );
      }
    } finally {
      if (mounted.current) setRefreshing(false);
      inFlight.current = false;
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    queueMicrotask(() => {
      if (mounted.current) void refresh();
    });
    const timer = window.setInterval(refreshWhenVisible, GITHUB_REFRESH_INTERVAL_MS);
    window.addEventListener("focus", refreshWhenVisible);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", refreshWhenVisible);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
      mounted.current = false;
    };
  }, [refresh]);

  return { states, refreshing, refresh };
}

function SolutionStatus() {
  return (
    <span
      data-solution-status="done"
      className="project-data inline-flex items-center gap-1 rounded-full border border-green-200 bg-green-50 px-2 py-0.5 text-[9px] font-semibold text-green-700"
    >
      <CheckCircle2 className="h-3 w-3" />
      DONE
    </span>
  );
}

function UpstreamStatus({
  status,
}: {
  status: LivePullRequestState;
}) {
  const title = status.stale
    ? "Showing the last known state; GitHub refresh is pending or unavailable."
    : `Live GitHub state checked ${new Date(status.checkedAt ?? "").toLocaleString()}`;
  return (
    <span
      data-upstream-state={status.state}
      title={title}
      className={cn(
        "project-data inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[9px] font-semibold uppercase",
        UPSTREAM_TONES[status.state],
      )}
    >
      <GitPullRequest className="h-3 w-3" />
      PR {status.state}
      <span
        aria-label={status.stale ? "Status not yet refreshed" : "Live GitHub status"}
        className={cn(
          "h-1.5 w-1.5 rounded-full",
          status.stale ? "bg-current opacity-40" : "bg-emerald-500",
        )}
      />
    </span>
  );
}

function PullRequestLink({ solution }: { solution: CompletedSolution }) {
  return (
    <a
      href={solution.pullRequestUrl}
      target="_blank"
      rel="noreferrer"
      className="project-id inline-flex items-center gap-1 font-semibold text-blue-600 hover:underline"
    >
      #{solution.pullRequestNumber}
      <ExternalLink className="h-3 w-3" />
    </a>
  );
}

export function CompletedSolutionsTable() {
  const { states, refreshing, refresh } = useLivePullRequestStates();
  return (
    <ProjectPanel
      title={`Completed solutions · ${COMPLETED_SOLUTIONS.length}`}
      subtitle="Delivered fixes with their current upstream pull-request state"
      action={
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={refreshing}
          className="inline-flex items-center gap-1 text-[10px] font-semibold text-muted-foreground transition hover:text-blue-600 disabled:opacity-50"
          aria-label="Refresh GitHub pull request states"
        >
          <RefreshCw className={cn("h-3 w-3", refreshing && "animate-spin")} />
          GitHub
        </button>
      }
    >
      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] text-left text-[11px]">
          <thead className="border-b border-border bg-muted/30 text-[9px] uppercase tracking-[0.08em] text-muted-foreground">
            <tr>
              <th className="px-4 py-2.5 font-semibold">Repository / PR</th>
              <th className="px-3 py-2.5 font-semibold">Completed solution</th>
              <th className="px-3 py-2.5 font-semibold">Resolution</th>
              <th className="px-4 py-2.5 font-semibold">Upstream</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {COMPLETED_SOLUTIONS.map((solution) => (
              <tr
                key={solution.id}
                data-completed-solution={solution.id}
                className="transition hover:bg-muted/30"
              >
                <td className="px-4 py-3 align-top">
                  <p className="font-semibold">{solution.repository}</p>
                  <p className="mt-1 text-[10px]">
                    <PullRequestLink solution={solution} />
                  </p>
                </td>
                <td className="max-w-[38rem] px-3 py-3 align-top">
                  <p className="font-semibold">{solution.title}</p>
                  <p className="mt-1 leading-5 text-muted-foreground">
                    {solution.summary}
                  </p>
                </td>
                <td className="px-3 py-3 align-top">
                  <SolutionStatus />
                </td>
                <td className="px-4 py-3 align-top">
                  <UpstreamStatus status={states[solution.id]} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </ProjectPanel>
  );
}

export function CompletedSolutionsCards() {
  const { states, refreshing, refresh } = useLivePullRequestStates();
  return (
    <ProjectPanel
      title={`Completed solutions · ${COMPLETED_SOLUTIONS.length}`}
      subtitle="Finished engineering outcomes remain separate from the live Work queue"
      action={
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={refreshing}
          className="inline-flex items-center gap-1 text-[10px] font-semibold text-muted-foreground transition hover:text-blue-600 disabled:opacity-50"
          aria-label="Refresh GitHub pull request states"
        >
          <RefreshCw className={cn("h-3 w-3", refreshing && "animate-spin")} />
          GitHub
        </button>
      }
      bodyClassName="grid gap-3 p-4 md:grid-cols-2 2xl:grid-cols-4"
    >
      {COMPLETED_SOLUTIONS.map((solution) => (
        <article
          key={solution.id}
          data-completed-solution={solution.id}
          className="rounded-md border border-border bg-slate-50/60 p-3"
        >
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="truncate text-[10px] font-semibold text-muted-foreground">
                {solution.repository}
              </p>
              <p className="mt-1 text-[10px]">
                <PullRequestLink solution={solution} />
              </p>
            </div>
            <SolutionStatus />
          </div>
          <h3 className="mt-3 text-xs font-semibold leading-5">
            {solution.title}
          </h3>
          <p className="mt-1 line-clamp-3 text-[10px] leading-4 text-muted-foreground">
            {solution.summary}
          </p>
          <div className="mt-3">
            <UpstreamStatus status={states[solution.id]} />
          </div>
        </article>
      ))}
    </ProjectPanel>
  );
}
