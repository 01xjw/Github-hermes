import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import {
  Activity,
  Bot,
  CalendarDays,
  CheckCircle2,
  CircleDot,
  Clock3,
  ExternalLink,
  ListChecks,
  Play,
  RefreshCw,
  ScanSearch,
} from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { usePageHeader } from "@/contexts/usePageHeader";
import { api } from "@/lib/api";
import type {
  PollingOverviewResponse,
  ProjectWorkItem,
  ProjectWorkListResponse,
} from "@/lib/api";
import {
  formatProjectInterval,
  formatProjectTime,
  ProjectMetric,
  ProjectPanel,
  RepositorySelect,
  WorkStatusBadge,
} from "@/components/ProjectHermesUi";
import { CompletedSolutionsTable } from "@/components/CompletedSolutions";

const REFRESH_INTERVAL_MS = 10_000;

function RepositoryDistribution({
  overview,
  onSelect,
}: {
  overview: PollingOverviewResponse;
  onSelect: (repositoryId: number) => void;
}) {
  const maxIssues = Math.max(
    1,
    ...overview.repository_stats.map((repository) => repository.issue_count),
  );

  return (
    <ProjectPanel
      title="Repository distribution"
      subtitle="Unique Issues retained after each one-day repository scan"
      bodyClassName="space-y-3 px-4 py-4"
    >
      {overview.repository_stats.length ? (
        overview.repository_stats.map((repository) => {
          const selected = overview.selected_repository_id === repository.repository_id;
          const width = Math.max(2, (repository.issue_count / maxIssues) * 100);
          return (
            <button
              key={repository.repository_id}
              type="button"
              className="grid w-full grid-cols-[minmax(7.5rem,0.75fr)_minmax(7rem,1.5fr)_4.5rem] items-center gap-3 text-left"
              onClick={() => onSelect(repository.repository_id)}
            >
              <span className="truncate text-[11px] font-semibold">
                {repository.repository}
                <span className="project-id ml-1.5 text-[9px] font-normal text-muted-foreground">
                  {repository.repository_id}
                </span>
              </span>
              <span className="h-2 overflow-hidden rounded-full bg-muted">
                <span
                  className={`block h-full rounded-full transition-[width] ${
                    selected ? "bg-blue-500" : "bg-teal-500/80"
                  }`}
                  style={{ width: `${width}%` }}
                />
              </span>
              <span className="project-data text-right text-[10px] text-muted-foreground">
                {repository.issue_count} · {repository.screening.SELECT} select
              </span>
            </button>
          );
        })
      ) : (
        <p className="py-12 text-center text-xs text-muted-foreground">
          Repository metrics appear after the first polling run.
        </p>
      )}
    </ProjectPanel>
  );
}

function RecentWorkTable({ items }: { items: ProjectWorkItem[] }) {
  return (
    <ProjectPanel
      title="Recent Work"
      subtitle="Main Hermes plans and worker execution state"
      action={
        <Link to="/work" className="text-[11px] font-semibold text-blue-600 hover:underline">
          View queue
        </Link>
      }
    >
      <div className="overflow-x-auto">
        <table className="w-full min-w-[720px] text-left text-[11px]">
          <thead className="border-b border-border bg-muted/30 text-[9px] uppercase tracking-[0.08em] text-muted-foreground">
            <tr>
              <th className="px-4 py-2.5 font-semibold">Repository</th>
              <th className="px-3 py-2.5 font-semibold">Issue</th>
              <th className="px-3 py-2.5 font-semibold">State</th>
              <th className="px-3 py-2.5 font-semibold">Current step</th>
              <th className="px-4 py-2.5 text-right font-semibold">Updated</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {items.length ? (
              items.map((item) => (
                <tr key={item.work_item_id} className="transition hover:bg-muted/30">
                  <td className="px-4 py-3">
                    <p className="font-semibold">{item.repository}</p>
                    <p className="project-id mt-0.5 text-[9px] text-muted-foreground">
                      repo {item.repository_id}
                    </p>
                  </td>
                  <td className="max-w-[16rem] px-3 py-3">
                    <a
                      href={item.issue_url}
                      target="_blank"
                      rel="noreferrer"
                      className="group/link block"
                    >
                      <span className="project-id mr-1.5 text-[10px] text-blue-600">
                        #{item.issue_number}
                      </span>
                      <span className="font-medium group-hover/link:text-blue-600">
                        {item.title}
                      </span>
                    </a>
                  </td>
                  <td className="px-3 py-3"><WorkStatusBadge status={item.status} /></td>
                  <td className="max-w-[20rem] px-3 py-3 text-muted-foreground">
                    <p className="line-clamp-2">{item.current_step}</p>
                  </td>
                  <td className="project-data px-4 py-3 text-right text-[9px] text-muted-foreground">
                    {formatProjectTime(item.updated_at)}
                  </td>
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={5} className="px-4 py-12 text-center text-xs text-muted-foreground">
                  Matched Issues will enter the Work queue here.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </ProjectPanel>
  );
}

export default function ProjectOverviewPage() {
  const { setEnd } = usePageHeader();
  const [overview, setOverview] = useState<PollingOverviewResponse | null>(null);
  const [work, setWork] = useState<ProjectWorkListResponse | null>(null);
  const [repositoryId, setRepositoryId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [runningPoll, setRunningPoll] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (background = false) => {
    if (background) setRefreshing(true);
    else setLoading(true);
    try {
      const [nextOverview, nextWork] = await Promise.all([
        api.getPollingOverview(repositoryId ?? undefined),
        api.listProjectWorkItems({
          repositoryId: repositoryId ?? undefined,
          limit: 8,
        }),
      ]);
      setOverview(nextOverview);
      setWork(nextWork);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [repositoryId]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(true), REFRESH_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const runNow = useCallback(async () => {
    setRunningPoll(true);
    setError(null);
    try {
      await api.runPollingNow();
      await refresh(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setRunningPoll(false);
    }
  }, [refresh]);

  useEffect(() => {
    setEnd(
      <div className="flex items-center gap-1">
        <Button
          ghost
          size="sm"
          type="button"
          onClick={() => void runNow()}
          disabled={loading || runningPoll}
          title="Scan the next repository now"
        >
          {runningPoll ? <Spinner /> : <Play className="h-3.5 w-3.5" />}
          <span className="hidden sm:inline">Poll next repo</span>
        </Button>
        <Button
          ghost
          size="icon"
          type="button"
          onClick={() => void refresh(true)}
          disabled={loading || refreshing}
          aria-label="Refresh ProjectHermes overview"
        >
          {loading || refreshing ? <Spinner /> : <RefreshCw />}
        </Button>
      </div>,
    );
    return () => setEnd(null);
  }, [loading, refresh, refreshing, runNow, runningPoll, setEnd]);

  const selectedRepository = useMemo(
    () => overview?.repository_stats.find((item) => item.repository_id === repositoryId),
    [overview, repositoryId],
  );
  const queued = (overview?.work_counts.queued ?? 0) + (overview?.work_counts.planning ?? 0);
  const active = overview?.work_counts.running ?? 0;

  if (loading && !overview) {
    return (
      <div className="project-hermes-ui flex min-h-[24rem] items-center justify-center">
        <Spinner />
      </div>
    );
  }

  return (
    <div className="project-hermes-ui space-y-4 py-4">
      <ProjectPanel bodyClassName="px-5 py-4">
        <div className="flex flex-col justify-between gap-4 xl:flex-row xl:items-center">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="inline-flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.1em] text-teal-600">
                <Activity className="h-3.5 w-3.5" />
                Live polling control plane
              </span>
              <span className={`project-data inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[9px] uppercase ${
                overview?.supervisor?.running
                  ? "border-green-200 bg-green-50 text-green-700"
                  : "border-amber-200 bg-amber-50 text-amber-700"
              }`}>
                <span className="h-1.5 w-1.5 rounded-full bg-current" />
                {overview?.supervisor?.running ? "supervisor online" : "supervisor offline"}
              </span>
            </div>
            <h2 className="mt-2 text-xl font-semibold">ProjectHermes Operations</h2>
            <p className="mt-1 max-w-3xl text-xs leading-5 text-muted-foreground">
              Repository scanning, generic machine-fit screening, operator confirmation,
              then Main Hermes planning across six isolated worker lanes.
            </p>
          </div>
          <div>
            <RepositorySelect
              repositories={overview?.repository_stats ?? []}
              value={repositoryId}
              onChange={setRepositoryId}
            />
          </div>
        </div>
      </ProjectPanel>

      {error ? (
        <div role="alert" className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700">
          {error}
        </div>
      ) : null}

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-6">
        <ProjectMetric
          label="Issues scanned"
          value={overview?.issues_seen ?? 0}
          detail={`${overview?.polling_run_count ?? 0} completed scans${selectedRepository ? ` · ${selectedRepository.repository}` : ""}`}
          icon={ScanSearch}
          tone="blue"
        />
        <ProjectMetric
          label={selectedRepository ? "Repository issues" : "Tracked issues"}
          value={overview?.candidate_counts.total ?? 0}
          detail={`${overview?.screening_counts.SELECT ?? 0} select · ${overview?.screening_counts.DEFER ?? 0} defer · ${overview?.screening_counts.PENDING ?? 0} pending`}
          icon={ListChecks}
          tone="teal"
        />
        <ProjectMetric
          label="Processed today"
          value={overview?.processed.today ?? 0}
          detail="Terminal Work since 00:00 UTC"
          icon={CheckCircle2}
          tone="violet"
        />
        <ProjectMetric
          label="Processed this week"
          value={overview?.processed.this_week ?? 0}
          detail="Monday through current time"
          icon={CalendarDays}
          tone="amber"
        />
        <ProjectMetric
          label="Queue depth"
          value={queued}
          detail={`${overview?.work_counts.queued ?? 0} queued · ${overview?.work_counts.planning ?? 0} planned`}
          icon={Clock3}
          tone="rose"
        />
        <ProjectMetric
          label="Execution lanes"
          value={`${active}/6`}
          detail={`${overview?.work_counts.running ?? 0} running · ${overview?.work_counts.review ?? 0} awaiting review`}
          icon={Bot}
          tone="slate"
        />
      </section>

      {overview ? (
        <section className="grid gap-4 xl:grid-cols-[minmax(0,1.25fr)_minmax(19rem,0.75fr)]">
          <RepositoryDistribution overview={overview} onSelect={setRepositoryId} />
          <ProjectPanel title="Polling & manager" subtitle="Durable orchestration state" bodyClassName="divide-y divide-border">
            <dl className="grid grid-cols-2 gap-4 px-4 py-4 text-[11px]">
              <div>
                <dt className="text-[9px] uppercase tracking-[0.08em] text-muted-foreground">Per-repo refresh</dt>
                <dd className="project-data mt-1 font-semibold">every {formatProjectInterval(overview.task.interval_seconds)}</dd>
              </div>
              <div>
                <dt className="text-[9px] uppercase tracking-[0.08em] text-muted-foreground">Next run</dt>
                <dd className="project-data mt-1 font-semibold">{formatProjectTime(overview.task.next_run_at)}</dd>
              </div>
              <div>
                <dt className="text-[9px] uppercase tracking-[0.08em] text-muted-foreground">Latest scan</dt>
                <dd className="mt-1 flex items-center gap-1.5 font-semibold">
                  <CircleDot className="h-3 w-3 text-teal-600" />
                  {overview.latest_run?.status ?? "not started"}
                </dd>
              </div>
              <div>
                <dt className="text-[9px] uppercase tracking-[0.08em] text-muted-foreground">Main Hermes</dt>
                <dd className="mt-1 font-semibold">{overview.manager?.runtime_status ?? "waiting"}</dd>
              </div>
              <div>
                <dt className="text-[9px] uppercase tracking-[0.08em] text-muted-foreground">Screening Subagent</dt>
                <dd className="project-data mt-1 font-semibold">{overview.screening_counts.PENDING} pending · {overview.supervisor?.screening_turns ?? 0} turns</dd>
              </div>
            </dl>
            <div className="px-4 py-3 text-[10px] text-muted-foreground">
              <p className="mb-2">The next repository starts immediately after a bounded scan; discovery pauses automatically when the Work buffer is full.</p>
              {overview.latest_run ? (
                <p className="flex items-center justify-between gap-3">
                  <span className="project-id truncate">{overview.latest_run.run_id}</span>
                  <span>{formatProjectTime(overview.latest_run.completed_at ?? overview.latest_run.started_at)}</span>
                </p>
              ) : (
                <p>Use “Poll next repo” to start the first one-day scan.</p>
              )}
              {overview.latest_run?.error ? <p className="mt-2 text-red-700">{overview.latest_run.error}</p> : null}
              {overview.manager?.last_error ? <p className="mt-2 text-red-700">{overview.manager.last_error}</p> : null}
            </div>
            <div className="grid grid-cols-2 gap-2 px-4 py-3">
              <Link to="/issues" className="inline-flex items-center justify-center gap-1.5 rounded-md border border-border px-3 py-2 text-[10px] font-semibold transition hover:border-blue-500 hover:text-blue-600">
                Open Issue feed
              </Link>
              <a href={selectedRepository?.html_url ?? "https://github.com"} target="_blank" rel="noreferrer" className="inline-flex items-center justify-center gap-1.5 rounded-md border border-border px-3 py-2 text-[10px] font-semibold transition hover:border-blue-500 hover:text-blue-600">
                GitHub <ExternalLink className="h-3 w-3" />
              </a>
            </div>
          </ProjectPanel>
        </section>
      ) : null}

      <CompletedSolutionsTable />

      <RecentWorkTable items={work?.work_items ?? []} />
    </div>
  );
}
