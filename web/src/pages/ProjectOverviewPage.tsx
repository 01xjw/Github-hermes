import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Activity,
  Bot,
  CalendarDays,
  CheckCircle2,
  Clock3,
  ListChecks,
  Play,
  RefreshCw,
  ScanSearch
} from 'lucide-react'
import { Button } from '@nous-research/ui/ui/components/button'
import { Spinner } from '@nous-research/ui/ui/components/spinner'
import { usePageHeader } from '@/contexts/usePageHeader'
import { api } from '@/lib/api'
import type { PollingOverviewResponse, ProjectIssueFeedItem } from '@/lib/api'
import { ProjectMetric, ProjectPanel, RepositorySelect } from '@/components/ProjectHermesUi'
import { DailyThroughputChart } from '@/components/DailyThroughputChart'
import { ProjectPipelineResults, RepositoryCoverageTable } from '@/components/ProjectResultsOverview'
import { buildDailyThroughput, type DailyThroughputPoint } from '@/lib/project-throughput'

const REFRESH_INTERVAL_MS = 10_000
const THROUGHPUT_REFRESH_INTERVAL_MS = 5 * 60_000
const THROUGHPUT_PAGE_SIZE = 500

async function loadThroughputIssues(repositoryId: number | null) {
  const issues: ProjectIssueFeedItem[] = []
  let offset = 0
  let total = Number.POSITIVE_INFINITY

  while (offset < total) {
    const page = await api.listProjectIssues({
      repositoryId: repositoryId ?? undefined,
      limit: THROUGHPUT_PAGE_SIZE,
      offset
    })
    issues.push(...page.issues)
    total = page.total
    if (!page.issues.length) break
    offset += page.issues.length
  }

  return issues
}

export default function ProjectOverviewPage() {
  const { setEnd } = usePageHeader()
  const [overview, setOverview] = useState<PollingOverviewResponse | null>(null)
  const [repositoryId, setRepositoryId] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [runningPoll, setRunningPoll] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [throughput, setThroughput] = useState<{
    repositoryId: number | null
    data: DailyThroughputPoint[]
  } | null>(null)
  const [throughputLoading, setThroughputLoading] = useState(true)
  const [throughputError, setThroughputError] = useState<string | null>(null)

  const refresh = useCallback(
    async (background = false) => {
      if (background) setRefreshing(true)
      else setLoading(true)
      try {
        const nextOverview = await api.getPollingOverview(repositoryId ?? undefined)
        setOverview(nextOverview)
        setError(null)
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : String(reason))
      } finally {
        setLoading(false)
        setRefreshing(false)
      }
    },
    [repositoryId]
  )

  useEffect(() => {
    queueMicrotask(() => void refresh())
    const timer = window.setInterval(() => void refresh(true), REFRESH_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [refresh])

  useEffect(() => {
    let active = true
    let requestInFlight = false

    const refreshThroughput = async () => {
      if (requestInFlight) return
      requestInFlight = true
      setThroughputLoading(true)
      try {
        const issues = await loadThroughputIssues(repositoryId)
        if (!active) return
        setThroughput({
          repositoryId,
          data: buildDailyThroughput(issues)
        })
        setThroughputError(null)
      } catch (reason) {
        if (active) {
          setThroughputError(reason instanceof Error ? reason.message : String(reason))
        }
      } finally {
        requestInFlight = false
        if (active) setThroughputLoading(false)
      }
    }
    const refreshWhenVisible = () => {
      if (document.visibilityState === 'visible') void refreshThroughput()
    }

    void refreshThroughput()
    const timer = window.setInterval(refreshWhenVisible, THROUGHPUT_REFRESH_INTERVAL_MS)
    window.addEventListener('focus', refreshWhenVisible)
    document.addEventListener('visibilitychange', refreshWhenVisible)
    return () => {
      active = false
      window.clearInterval(timer)
      window.removeEventListener('focus', refreshWhenVisible)
      document.removeEventListener('visibilitychange', refreshWhenVisible)
    }
  }, [repositoryId])

  const runNow = useCallback(async () => {
    setRunningPoll(true)
    setError(null)
    try {
      await api.runPollingNow()
      await refresh(true)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setRunningPoll(false)
    }
  }, [refresh])

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
      </div>
    )
    return () => setEnd(null)
  }, [loading, refresh, refreshing, runNow, runningPoll, setEnd])

  const selectedRepository = useMemo(
    () => overview?.repository_stats.find(item => item.repository_id === repositoryId),
    [overview, repositoryId]
  )
  const queued = (overview?.work_counts.queued ?? 0) + (overview?.work_counts.planning ?? 0)
  const active = overview?.work_counts.running ?? 0

  if (loading && !overview) {
    return (
      <div className="project-hermes-ui flex min-h-[24rem] items-center justify-center">
        <Spinner />
      </div>
    )
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
              <span
                className={`project-data inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[9px] uppercase ${
                  overview?.supervisor?.running
                    ? 'border-green-200 bg-green-50 text-green-700'
                    : 'border-amber-200 bg-amber-50 text-amber-700'
                }`}
              >
                <span className="h-1.5 w-1.5 rounded-full bg-current" />
                {overview?.supervisor?.running ? 'supervisor online' : 'supervisor offline'}
              </span>
            </div>
            <h2 className="mt-2 text-xl font-semibold">ProjectHermes Operations</h2>
            <p className="mt-1 max-w-3xl text-xs leading-5 text-muted-foreground">
              {`Repository scanning, generic machine-fit screening, ${
                overview?.operator_selection_required ? 'operator confirmation' : 'automatic policy admission'
              }, then Main Hermes planning across six isolated worker lanes.`}
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
          detail={`${overview?.polling_run_count ?? 0} completed scans${selectedRepository ? ` · ${selectedRepository.repository}` : ''}`}
          icon={ScanSearch}
          tone="blue"
        />
        <ProjectMetric
          label={selectedRepository ? 'Repository issues' : 'Tracked issues'}
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

      {overview ? <ProjectPipelineResults overview={overview} /> : null}

      <DailyThroughputChart
        data={throughput?.repositoryId === repositoryId ? throughput.data : null}
        loading={throughputLoading}
        error={throughputError}
      />

      {overview ? (
        <RepositoryCoverageTable repositories={overview.repository_stats} onSelect={setRepositoryId} />
      ) : null}
    </div>
  )
}
