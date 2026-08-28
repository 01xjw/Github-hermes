import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Box,
  CheckCircle2,
  Clock3,
  ExternalLink,
  ListTodo,
  RefreshCw,
  Search,
  ServerCog,
  ShieldCheck
} from 'lucide-react'
import { Button } from '@nous-research/ui/ui/components/button'
import { Spinner } from '@nous-research/ui/ui/components/spinner'
import { usePageHeader } from '@/contexts/usePageHeader'
import { api } from '@/lib/api'
import type {
  PollingOverviewResponse,
  ProjectIssueDetailResponse,
  ProjectWorkDetailResponse,
  ProjectWorkItem,
  ProjectWorkListResponse,
  ProjectWorkStatus
} from '@/lib/api'
import {
  EnvironmentBadge,
  formatProjectTime,
  ProjectPanel,
  RepositorySelect,
  WorkStatusBadge
} from '@/components/ProjectHermesUi'
import { CompletedSolutionsTable } from '@/components/CompletedSolutions'
import { IssueDetail } from '@/pages/IssuesPage'
import { cn } from '@/lib/utils'

const REFRESH_INTERVAL_MS = 5_000
type StatusFilter = ProjectWorkStatus | 'all'

const STATUS_FILTERS: StatusFilter[] = ['all', 'queued', 'planning', 'running', 'review', 'done', 'blocked', 'failed']

function WorkDetail({ detail }: { detail: ProjectWorkDetailResponse | null }) {
  if (!detail) {
    return (
      <div className="flex min-h-64 items-center justify-center px-6 text-center text-xs text-muted-foreground">
        Select a Work item to inspect its plan, execution identities, and timeline.
      </div>
    )
  }
  const { work_item: work, candidate, events } = detail
  return (
    <div className="divide-y divide-border">
      <section className="px-5 py-4">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="project-id text-[10px] font-semibold text-blue-600">
              {work.repository} · repo {work.repository_id} · #{work.issue_number}
            </p>
            <h2 className="mt-2 text-base font-semibold leading-6">{work.title}</h2>
          </div>
          <a
            href={work.issue_url}
            target="_blank"
            rel="noreferrer"
            className="shrink-0 rounded-md border border-border p-2 text-muted-foreground transition hover:border-blue-500 hover:text-blue-600"
            aria-label="Open source Issue"
          >
            <ExternalLink className="h-3.5 w-3.5" />
          </a>
        </div>
        <div className="mt-3 flex flex-wrap gap-2">
          <WorkStatusBadge status={work.status} />
          <EnvironmentBadge status={work.environment_status} />
        </div>
        <p className="mt-3 text-xs leading-5">{work.current_step}</p>
        {work.retry_not_before ? (
          <p className="project-data mt-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[10px] text-amber-800">
            Provider-capacity retry becomes eligible {formatProjectTime(work.retry_not_before)}. The committed plan is
            preserved.
          </p>
        ) : null}
      </section>

      <section className="grid gap-4 px-5 py-4 sm:grid-cols-2">
        <div>
          <h3 className="text-[9px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Work identity</h3>
          <dl className="mt-2 space-y-1.5 text-[10px]">
            <div className="flex justify-between gap-3">
              <dt className="text-muted-foreground">Work</dt>
              <dd className="project-id truncate">{work.work_item_id}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-muted-foreground">Task</dt>
              <dd className="project-id truncate">{work.task_id ?? '—'}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-muted-foreground">Run</dt>
              <dd className="project-id truncate">{work.run_id ?? '—'}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-muted-foreground">Execution</dt>
              <dd className="project-id truncate">{work.execution_id ?? '—'}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-muted-foreground">Attempt</dt>
              <dd className="project-data">{work.execution_attempt || '—'}</dd>
            </div>
          </dl>
        </div>
        <div>
          <h3 className="text-[9px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Timing</h3>
          <dl className="mt-2 space-y-1.5 text-[10px]">
            <div className="flex justify-between gap-3">
              <dt className="text-muted-foreground">Queued</dt>
              <dd className="project-data">{formatProjectTime(work.queued_at)}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-muted-foreground">Started</dt>
              <dd className="project-data">{formatProjectTime(work.started_at)}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-muted-foreground">Review</dt>
              <dd className="project-data">{formatProjectTime(work.review_started_at)}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-muted-foreground">Completed</dt>
              <dd className="project-data">{formatProjectTime(work.completed_at)}</dd>
            </div>
            <div className="flex justify-between gap-3">
              <dt className="text-muted-foreground">Retry eligible</dt>
              <dd className="project-data">{formatProjectTime(work.retry_not_before)}</dd>
            </div>
          </dl>
        </div>
      </section>

      {work.plan ? (
        <section className="px-5 py-4">
          <h3 className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            <ListTodo className="h-3.5 w-3.5" /> Committed plan
          </h3>
          <p className="mt-2 text-xs leading-5">{work.plan.summary}</p>
          <ol className="mt-3 space-y-2">
            {work.plan.steps.map((step, index) => (
              <li key={`${index}-${step}`} className="flex gap-2 text-xs leading-5 text-muted-foreground">
                <span className="project-data flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-blue-50 text-[9px] font-semibold text-blue-700">
                  {index + 1}
                </span>
                {step}
              </li>
            ))}
          </ol>
          <h4 className="mt-4 flex items-center gap-1.5 text-[9px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            <ShieldCheck className="h-3.5 w-3.5" /> Acceptance criteria
          </h4>
          <ul className="mt-2 space-y-1.5">
            {work.plan.acceptance_criteria.map(criterion => (
              <li key={criterion} className="flex gap-2 text-[11px] leading-5 text-muted-foreground">
                <CheckCircle2 className="mt-1 h-3 w-3 shrink-0 text-green-600" />
                {criterion}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {work.environment_status === 'verified' && work.resource_requirements ? (
        <section className="px-5 py-4">
          <h3 className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            <ServerCog className="h-3.5 w-3.5" /> Verified execution resources
          </h3>
          <dl className="mt-3 grid grid-cols-2 gap-3 text-[10px]">
            <div className="rounded-md bg-muted/30 p-2.5">
              <dt className="text-muted-foreground">CPU request / limit</dt>
              <dd className="project-data mt-1 font-semibold">
                {work.resource_requirements.cpu_request} / {work.resource_requirements.cpu_limit}
              </dd>
            </div>
            <div className="rounded-md bg-muted/30 p-2.5">
              <dt className="text-muted-foreground">Memory request / limit</dt>
              <dd className="project-data mt-1 font-semibold">
                {work.resource_requirements.memory_request} / {work.resource_requirements.memory_limit}
              </dd>
            </div>
            <div className="rounded-md bg-muted/30 p-2.5">
              <dt className="text-muted-foreground">GPU</dt>
              <dd className="project-data mt-1 font-semibold">
                {work.resource_requirements.gpu_count} · {work.resource_requirements.gpu_architecture ?? 'none'}
              </dd>
            </div>
            <div className="rounded-md bg-muted/30 p-2.5">
              <dt className="text-muted-foreground">Worker model</dt>
              <dd className="project-data mt-1 break-all font-semibold">{work.resource_requirements.worker_model}</dd>
            </div>
          </dl>
          <p className="project-id mt-3 text-[9px] text-muted-foreground">named baseline {work.named_baseline}</p>
        </section>
      ) : null}

      <section className="px-5 py-4">
        <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Event timeline</h3>
        {events.length ? (
          <ol className="mt-3 space-y-3 border-l border-border pl-4">
            {events
              .slice()
              .reverse()
              .map(event => (
                <li key={event.event_id} className="relative">
                  <span className="absolute -left-[1.18rem] top-1 h-2 w-2 rounded-full border border-blue-500 bg-card" />
                  <div className="flex items-center justify-between gap-3">
                    <span className="project-id text-[10px] font-semibold">{event.event_type}</span>
                    <time className="project-data text-[9px] text-muted-foreground">
                      {formatProjectTime(event.created_at)}
                    </time>
                  </div>
                </li>
              ))}
          </ol>
        ) : (
          <p className="mt-3 text-xs text-muted-foreground">No Work events recorded.</p>
        )}
        {candidate.filter_reasons.length ? (
          <p className="mt-4 text-[10px] text-muted-foreground">Filter: {candidate.filter_reasons.join(' · ')}</p>
        ) : null}
      </section>
    </div>
  )
}

function RecentWorkTable({
  items,
  selectedId,
  onSelect
}: {
  items: ProjectWorkItem[]
  selectedId: string | null
  onSelect: (item: ProjectWorkItem) => void
}) {
  return (
    <ProjectPanel
      title={`Recent Work · ${items.length}`}
      subtitle="Most recently updated plans, workers, and review outcomes"
    >
      <div className="overflow-x-auto">
        <table className="w-full min-w-[780px] text-left text-[11px]">
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
              items.map(item => (
                <tr
                  key={item.work_item_id}
                  onClick={() => onSelect(item)}
                  className={cn(
                    'cursor-pointer transition hover:bg-muted/30',
                    selectedId === item.work_item_id && 'bg-blue-50'
                  )}
                >
                  <td className="px-4 py-3 font-semibold">{item.repository}</td>
                  <td className="max-w-[18rem] px-3 py-3">
                    <a
                      href={item.issue_url}
                      target="_blank"
                      rel="noreferrer"
                      onClick={event => event.stopPropagation()}
                      className="group/link block"
                    >
                      <span className="project-id mr-1.5 text-[10px] text-blue-600">#{item.issue_number}</span>
                      <span className="font-medium group-hover/link:text-blue-600">{item.title}</span>
                    </a>
                  </td>
                  <td className="px-3 py-3">
                    <WorkStatusBadge status={item.status} />
                  </td>
                  <td className="max-w-[24rem] px-3 py-3 text-muted-foreground">
                    <p className="line-clamp-2">{item.current_step}</p>
                  </td>
                  <td className="project-data whitespace-nowrap px-4 py-3 text-right text-[9px] text-muted-foreground">
                    {formatProjectTime(item.updated_at)}
                  </td>
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={5} className="px-4 py-10 text-center text-xs text-muted-foreground">
                  No Work items have been admitted yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </ProjectPanel>
  )
}

export default function WorkPage() {
  const { setEnd } = usePageHeader()
  const [overview, setOverview] = useState<PollingOverviewResponse | null>(null)
  const [work, setWork] = useState<ProjectWorkListResponse | null>(null)
  const [recentWork, setRecentWork] = useState<ProjectWorkItem[]>([])
  const [workDetail, setWorkDetail] = useState<ProjectWorkDetailResponse | null>(null)
  const [issueDetail, setIssueDetail] = useState<ProjectIssueDetailResponse | null>(null)
  const [repositoryId, setRepositoryId] = useState<number | null>(null)
  const [workStatus, setWorkStatus] = useState<StatusFilter>('all')
  const [workSearch, setWorkSearch] = useState('')
  const [selectedCandidateId, setSelectedCandidateId] = useState<string | null>(null)
  const [selectedWorkId, setSelectedWorkId] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [issueDetailLoading, setIssueDetailLoading] = useState(false)
  const [selecting, setSelecting] = useState(false)
  const [rescreening, setRescreening] = useState(false)
  const [rescreenDraft, setRescreenDraft] = useState({
    candidateId: null as string | null,
    reason: '',
    probeEvidence: ''
  })
  const [error, setError] = useState<string | null>(null)
  const selectionInitialized = useRef(false)

  const refresh = useCallback(
    async (background = false) => {
      if (background) setRefreshing(true)
      else setLoading(true)
      try {
        const [nextWork, nextRecentWork, nextOverview] = await Promise.all([
          api.listProjectWorkItems({
            repositoryId: repositoryId ?? undefined,
            status: workStatus === 'all' ? undefined : [workStatus],
            limit: 200
          }),
          api.listProjectWorkItems({
            repositoryId: repositoryId ?? undefined,
            limit: 8
          }),
          api.getPollingOverview()
        ])
        setWork(nextWork)
        setRecentWork(nextRecentWork.work_items)
        setOverview(nextOverview)
        if (!selectionInitialized.current) {
          const initialWork = nextWork.work_items[0] ?? nextRecentWork.work_items[0]
          setSelectedWorkId(initialWork?.work_item_id ?? null)
          setSelectedCandidateId(initialWork?.candidate_id ?? null)
          setIssueDetailLoading(Boolean(initialWork))
          selectionInitialized.current = Boolean(initialWork)
        }
        setError(null)
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : String(reason))
      } finally {
        setLoading(false)
        setRefreshing(false)
      }
    },
    [repositoryId, workStatus]
  )

  useEffect(() => {
    queueMicrotask(() => void refresh())
    const timer = window.setInterval(() => void refresh(true), REFRESH_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [refresh])

  useEffect(() => {
    if (!selectedWorkId) return
    let active = true
    api
      .getProjectWorkItem(selectedWorkId)
      .then(next => {
        if (active) setWorkDetail(next)
      })
      .catch((reason: unknown) => {
        if (active) {
          setError(reason instanceof Error ? reason.message : String(reason))
        }
      })
    return () => {
      active = false
    }
  }, [selectedWorkId, work])

  useEffect(() => {
    if (!selectedCandidateId) return
    let active = true
    api
      .getProjectIssue(selectedCandidateId)
      .then(next => {
        if (active) setIssueDetail(next)
      })
      .catch((reason: unknown) => {
        if (active) {
          setError(reason instanceof Error ? reason.message : String(reason))
        }
      })
      .finally(() => {
        if (active) setIssueDetailLoading(false)
      })
    return () => {
      active = false
    }
  }, [selectedCandidateId, work])

  useEffect(() => {
    setEnd(
      <Button
        ghost
        size="icon"
        type="button"
        onClick={() => void refresh(true)}
        disabled={loading || refreshing}
        aria-label="Refresh Work queue"
      >
        {loading || refreshing ? <Spinner /> : <RefreshCw />}
      </Button>
    )
    return () => setEnd(null)
  }, [loading, refresh, refreshing, setEnd])

  const filteredItems = useMemo(() => {
    const needle = workSearch.trim().toLocaleLowerCase()
    if (!needle) return work?.work_items ?? []
    return (work?.work_items ?? []).filter(item =>
      `${item.repository} ${item.repository_id} ${item.issue_number} ${item.title} ${item.current_step}`
        .toLocaleLowerCase()
        .includes(needle)
    )
  }, [work, workSearch])

  const activeRescreenReason = rescreenDraft.candidateId === selectedCandidateId ? rescreenDraft.reason : ''
  const activeProbeEvidence = rescreenDraft.candidateId === selectedCandidateId ? rescreenDraft.probeEvidence : ''

  const selectWork = useCallback((item: ProjectWorkItem) => {
    setIssueDetailLoading(true)
    setSelectedWorkId(item.work_item_id)
    setSelectedCandidateId(item.candidate_id)
  }, [])

  const changeRepository = useCallback((value: number | null) => {
    selectionInitialized.current = false
    setIssueDetailLoading(true)
    setSelectedCandidateId(null)
    setSelectedWorkId(null)
    setRepositoryId(value)
  }, [])

  const selectForWork = useCallback(async () => {
    if (!issueDetail?.operator_selectable) return
    const approved = window.confirm(
      `Approve ${issueDetail.candidate.repository} #${issueDetail.candidate.issue_number} for Main Hermes planning and Codex execution?`
    )
    if (!approved) return
    setSelecting(true)
    try {
      const next = await api.selectProjectIssue(issueDetail.candidate.candidate_id)
      setIssueDetail(next)
      if (next.work_item) selectWork(next.work_item)
      await refresh(true)
      setError(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setSelecting(false)
    }
  }, [issueDetail, refresh, selectWork])

  const rescreenIssue = useCallback(async () => {
    if (!issueDetail?.screening || issueDetail.work_item) return
    const reason = activeRescreenReason.trim()
    const evidence = activeProbeEvidence
      .split('\n')
      .map(value => value.trim())
      .filter(Boolean)
    if (!reason || !evidence.length) return
    setRescreening(true)
    try {
      const next = await api.rescreenProjectIssue(issueDetail.candidate.candidate_id, {
        reason,
        probe_evidence: evidence
      })
      setIssueDetail(next)
      setRescreenDraft({ candidateId: null, reason: '', probeEvidence: '' })
      await refresh(true)
      setError(null)
    } catch (reasonValue) {
      setError(reasonValue instanceof Error ? reasonValue.message : String(reasonValue))
    } finally {
      setRescreening(false)
    }
  }, [activeProbeEvidence, activeRescreenReason, issueDetail, refresh])

  const activeCount = work?.counts.running ?? 0

  return (
    <div className="project-hermes-ui space-y-4 py-4">
      <ProjectPanel bodyClassName="px-4 py-3">
        <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
          <div>
            <p className="text-[10px] font-semibold uppercase tracking-[0.1em] text-violet-700">
              Main Hermes work graph
            </p>
            <h2 className="mt-1 text-lg font-semibold">Work operations</h2>
            <p className="mt-1 text-[11px] text-muted-foreground">
              Issue intake → committed plan → isolated Kubernetes worker → review
            </p>
          </div>
          <RepositorySelect
            repositories={overview?.repository_stats ?? []}
            value={repositoryId}
            onChange={changeRepository}
          />
        </div>
        <div className="mt-3 flex justify-end border-t border-border pt-3">
          <span className="project-data inline-flex items-center justify-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-[10px]">
            <Box className="h-3.5 w-3.5 text-blue-600" /> {activeCount}/6 execution lanes
          </span>
        </div>
      </ProjectPanel>

      {error ? (
        <div role="alert" className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700">
          {error}
        </div>
      ) : null}

      <RecentWorkTable items={recentWork} selectedId={selectedWorkId} onSelect={selectWork} />

      <ProjectPanel
        title={`Work items · ${filteredItems.length}`}
        subtitle="Click a row to inspect durable state and execution evidence"
      >
        <div className="flex flex-col gap-2 border-b border-border px-4 py-3">
          <label className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <input
              value={workSearch}
              onChange={event => setWorkSearch(event.target.value)}
              placeholder="Search Work by repository, Issue, or current step"
              className="w-full rounded-md border border-border bg-card py-1.5 pl-8 pr-3 text-[11px] outline-none transition placeholder:text-muted-foreground focus:border-blue-500"
            />
          </label>
          <div className="scrollbar-none flex gap-1 overflow-x-auto pb-1">
            {STATUS_FILTERS.map(value => {
              const count =
                value === 'all'
                  ? Object.values(work?.counts ?? {}).reduce((sum, item) => sum + item, 0)
                  : (work?.counts[value] ?? 0)
              return (
                <button
                  key={value}
                  type="button"
                  onClick={() => setWorkStatus(value)}
                  className={cn(
                    'project-data inline-flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-[9px] font-semibold uppercase transition',
                    workStatus === value
                      ? 'border-blue-500 bg-blue-500 text-white'
                      : 'border-border bg-card text-muted-foreground hover:border-blue-500 hover:text-blue-600'
                  )}
                >
                  {value}
                  <span className="rounded-full bg-current/10 px-1.5 py-0.5">{count}</span>
                </button>
              )
            })}
          </div>
        </div>
        <div className="max-h-[36rem] min-h-[28rem] overflow-auto">
          {loading && !work ? (
            <div className="flex min-h-[28rem] items-center justify-center">
              <Spinner />
            </div>
          ) : (
            <table className="w-full min-w-[820px] text-left text-[11px]">
              <thead className="sticky top-0 z-10 border-b border-border bg-card text-[9px] uppercase tracking-[0.08em] text-muted-foreground">
                <tr>
                  <th className="px-4 py-2.5 font-semibold">Issue</th>
                  <th className="px-3 py-2.5 font-semibold">State</th>
                  <th className="px-3 py-2.5 font-semibold">Environment</th>
                  <th className="px-3 py-2.5 font-semibold">Current step</th>
                  <th className="px-4 py-2.5 text-right font-semibold">Updated</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {filteredItems.length ? (
                  filteredItems.map((item: ProjectWorkItem) => (
                    <tr
                      key={item.work_item_id}
                      onClick={() => selectWork(item)}
                      className={cn(
                        'cursor-pointer transition hover:bg-slate-50',
                        selectedWorkId === item.work_item_id && 'bg-blue-50'
                      )}
                    >
                      <td className="max-w-[17rem] px-4 py-3">
                        <p className="truncate font-semibold">{item.title}</p>
                        <p className="project-id mt-1 text-[9px] text-muted-foreground">
                          {item.repository} · {item.repository_id} · #{item.issue_number}
                        </p>
                      </td>
                      <td className="px-3 py-3">
                        <WorkStatusBadge status={item.status} />
                      </td>
                      <td className="px-3 py-3">
                        <EnvironmentBadge status={item.environment_status} />
                      </td>
                      <td className="max-w-[22rem] px-3 py-3 text-muted-foreground">
                        <p className="line-clamp-2">{item.current_step}</p>
                      </td>
                      <td className="project-data px-4 py-3 text-right text-[9px] text-muted-foreground">
                        {formatProjectTime(item.updated_at)}
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={5} className="px-4 py-16 text-center text-xs text-muted-foreground">
                      <Clock3 className="mx-auto mb-3 h-6 w-6 opacity-50" />
                      No Work items match this view.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          )}
        </div>
      </ProjectPanel>

      <section id="work-details" className="grid scroll-mt-4 gap-4 2xl:grid-cols-2">
        <ProjectPanel title="Issue detail" subtitle="Evidence, screening decision, and admission state">
          <div className="max-h-[52rem] overflow-y-auto">
            <IssueDetail
              detail={selectedCandidateId ? issueDetail : null}
              loading={issueDetailLoading}
              selecting={selecting}
              rescreening={rescreening}
              rescreenReason={activeRescreenReason}
              probeEvidence={activeProbeEvidence}
              onRescreenReasonChange={value =>
                setRescreenDraft(current => ({
                  candidateId: selectedCandidateId,
                  reason: value,
                  probeEvidence: current.candidateId === selectedCandidateId ? current.probeEvidence : ''
                }))
              }
              onProbeEvidenceChange={value =>
                setRescreenDraft(current => ({
                  candidateId: selectedCandidateId,
                  reason: current.candidateId === selectedCandidateId ? current.reason : '',
                  probeEvidence: value
                }))
              }
              onRescreen={() => void rescreenIssue()}
              onSelectForWork={() => void selectForWork()}
            />
          </div>
        </ProjectPanel>
        <ProjectPanel title="Work detail" subtitle="Plan, resources, controller identities, and events">
          <div className="max-h-[52rem] overflow-y-auto">
            <WorkDetail detail={selectedWorkId ? workDetail : null} />
          </div>
        </ProjectPanel>
      </section>

      <CompletedSolutionsTable onSelect={selectWork} />
    </div>
  )
}
