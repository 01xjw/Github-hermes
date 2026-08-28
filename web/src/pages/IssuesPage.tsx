import { useCallback, useDeferredValue, useEffect, useMemo, useState } from 'react'
import {
  CheckCircle2,
  Bot,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Filter,
  GitPullRequest,
  MessageCircle,
  RefreshCw,
  Search,
  ServerCog,
  Tag,
  UserCheck
} from 'lucide-react'
import { Button } from '@nous-research/ui/ui/components/button'
import { Spinner } from '@nous-research/ui/ui/components/spinner'
import { usePageHeader } from '@/contexts/usePageHeader'
import { api } from '@/lib/api'
import type {
  PollingOverviewResponse,
  ProjectIssueDetailResponse,
  ProjectIssueFeedItem,
  ProjectIssueListResponse,
  ProjectScreeningFilter,
  ProjectWorkStatus
} from '@/lib/api'
import {
  EnvironmentBadge,
  formatProjectTime,
  ProjectPanel,
  RepositorySelect,
  WorkStatusBadge
} from '@/components/ProjectHermesUi'
import { cn } from '@/lib/utils'

const REFRESH_INTERVAL_MS = 5_000
const PAGE_SIZE = 50

type EligibilityFilter = 'matched' | 'filtered' | 'all'
type ScreeningFilter = ProjectScreeningFilter | 'all'
type StatusFilter = ProjectWorkStatus | 'all'

const STATUS_FILTERS: StatusFilter[] = ['all', 'queued', 'planning', 'running', 'review', 'done', 'blocked', 'failed']

function FeedStatus({ item }: { item: ProjectIssueFeedItem }) {
  if (item.work_item) return <WorkStatusBadge status={item.work_item.status} />
  if (item.screening) {
    const tone = {
      SELECT: 'border-green-200 bg-green-50 text-green-700',
      DEFER: 'border-amber-200 bg-amber-50 text-amber-700',
      REJECT: 'border-red-200 bg-red-50 text-red-700'
    }[item.screening.decision]
    return (
      <span className={cn('project-data inline-flex rounded-full border px-2 py-0.5 text-[10px] font-semibold', tone)}>
        {item.screening.decision}
      </span>
    )
  }
  return (
    <span
      className={cn(
        'project-data inline-flex rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase',
        item.candidate.eligible
          ? 'border-teal-200 bg-teal-50 text-teal-700'
          : 'border-slate-200 bg-slate-100 text-slate-600'
      )}
    >
      screening pending
    </span>
  )
}

export function IssueFeedRow({
  item,
  selected,
  onSelect
}: {
  item: ProjectIssueFeedItem
  selected: boolean
  onSelect: () => void
}) {
  const { candidate } = item
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        'block w-full border-b border-border px-4 py-3 text-left transition last:border-b-0 hover:bg-muted/30',
        selected && 'bg-blue-50 shadow-[inset_3px_0_0_#3b82f6]'
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] font-semibold text-muted-foreground">
            <span>{candidate.repository}</span>
            <span className="project-id font-normal">repo {candidate.repository_id}</span>
            <span className="project-id text-blue-600">#{candidate.issue_number}</span>
          </p>
          <h3 className="mt-1.5 line-clamp-2 text-[13px] font-semibold leading-5">{candidate.title}</h3>
        </div>
        <a
          href={candidate.issue_url}
          target="_blank"
          rel="noreferrer"
          onClick={event => event.stopPropagation()}
          className="shrink-0 rounded-md p-1.5 text-muted-foreground transition hover:bg-muted hover:text-blue-600"
          aria-label={`Open Issue ${candidate.issue_number} on GitHub`}
        >
          <ExternalLink className="h-3.5 w-3.5" />
        </a>
      </div>
      <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
        <FeedStatus item={item} />
        <span className="project-data rounded-full border border-border px-2 py-0.5 text-[9px] text-muted-foreground">
          score {candidate.evidence_score}
        </span>
        {candidate.labels.slice(0, 3).map(label => (
          <span
            key={label}
            className="max-w-28 truncate rounded-full bg-violet-50 px-2 py-0.5 text-[9px] font-medium text-violet-700"
          >
            {label}
          </span>
        ))}
        {candidate.labels.length > 3 ? (
          <span className="project-data text-[9px] text-muted-foreground">+{candidate.labels.length - 3}</span>
        ) : null}
      </div>
      <div className="mt-2 flex items-center gap-3 text-[9px] text-muted-foreground">
        <span className="inline-flex items-center gap-1">
          <MessageCircle className="h-3 w-3" /> {candidate.comments}
        </span>
        <span>seen {formatProjectTime(candidate.last_seen_at)}</span>
      </div>
    </button>
  )
}

export function IssueDetail({
  detail,
  loading,
  selecting,
  rescreening,
  rescreenReason,
  probeEvidence,
  onRescreenReasonChange,
  onProbeEvidenceChange,
  onRescreen,
  onSelectForWork
}: {
  detail: ProjectIssueDetailResponse | null
  loading: boolean
  selecting: boolean
  rescreening: boolean
  rescreenReason: string
  probeEvidence: string
  onRescreenReasonChange: (value: string) => void
  onProbeEvidenceChange: (value: string) => void
  onRescreen: () => void
  onSelectForWork: () => void
}) {
  if (loading && !detail) {
    return (
      <div className="flex min-h-[28rem] items-center justify-center">
        <Spinner />
      </div>
    )
  }
  if (!detail) {
    return (
      <div className="flex min-h-[28rem] items-center justify-center px-6 text-center text-xs text-muted-foreground">
        Select an Issue to inspect its evidence, plan, and Work history.
      </div>
    )
  }
  const { candidate, work_item: work, events } = detail

  return (
    <div className="divide-y divide-border">
      <section className="px-5 py-4">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="project-id text-[10px] font-semibold text-blue-600">
              {candidate.repository} / #{candidate.issue_number}
            </p>
            <h2 className="mt-2 text-lg font-semibold leading-6">{candidate.title}</h2>
          </div>
          <a
            href={candidate.issue_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-[10px] font-semibold transition hover:border-blue-500 hover:text-blue-600"
          >
            GitHub <ExternalLink className="h-3 w-3" />
          </a>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {work ? (
            <WorkStatusBadge status={work.status} />
          ) : (
            <FeedStatus item={{ candidate, work_item: null, screening: detail.screening }} />
          )}
          {work ? <EnvironmentBadge status={work.environment_status} /> : null}
          <span className="project-id text-[9px] text-muted-foreground">repo {candidate.repository_id}</span>
          <span className="text-[9px] text-muted-foreground">opened by {candidate.author ?? 'unknown'}</span>
        </div>
      </section>

      <section className="px-5 py-4">
        <h3 className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          <Bot className="h-3.5 w-3.5" /> Screening Subagent
        </h3>
        {detail.screening ? (
          <div className="mt-3 space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <FeedStatus item={{ candidate, work_item: null, screening: detail.screening }} />
              <span className="project-data rounded-full border border-border px-2 py-0.5 text-[9px] text-muted-foreground">
                {detail.screening.machine_compatibility}
              </span>
              <span className="project-data rounded-full border border-border px-2 py-0.5 text-[9px] text-muted-foreground">
                {detail.screening.task_kind}
              </span>
              <span className="project-data rounded-full border border-blue-200 bg-blue-50 px-2 py-0.5 text-[9px] text-blue-700">
                revision {detail.screening.revision}
              </span>
            </div>
            <p className="text-xs leading-5">{detail.screening.reason}</p>
            <dl className="grid gap-2 text-[10px] sm:grid-cols-2">
              <div>
                <dt className="text-muted-foreground">CPU / memory</dt>
                <dd className="project-data mt-1">
                  {detail.screening.required_environment.minimum_cpu_cores} cores ·{' '}
                  {detail.screening.required_environment.minimum_memory_gib} GiB
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">GPU</dt>
                <dd className="project-data mt-1">
                  {detail.screening.required_environment.gpu_count} ·{' '}
                  {detail.screening.required_environment.gpu_architectures.join(', ') || 'none'}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">OS / CPU arch</dt>
                <dd className="project-data mt-1">
                  {detail.screening.required_environment.operating_systems.join(', ') || 'any'} ·{' '}
                  {detail.screening.required_environment.cpu_architectures.join(', ') || 'any'}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">External requirements</dt>
                <dd className="project-data mt-1">
                  {detail.screening.required_environment.network_access_required ? 'network' : 'no network'} ·{' '}
                  {detail.screening.required_environment.external_system_write_required
                    ? 'external write'
                    : 'local only'}
                </dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="text-muted-foreground">Unresolved dependencies</dt>
                <dd className="project-data mt-1">
                  {detail.screening.required_environment.external_dependencies.join(', ') || 'none'}
                </dd>
              </div>
            </dl>
            {detail.screening.evidence.length ? (
              <ul className="list-disc space-y-1 pl-4 text-xs leading-5 text-muted-foreground">
                {detail.screening.evidence.map(value => (
                  <li key={value}>{value}</li>
                ))}
              </ul>
            ) : null}
            {detail.screening.uncertainties.length ? (
              <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[10px] leading-4 text-amber-800">
                Probe needed: {detail.screening.uncertainties.join(' · ')}
              </div>
            ) : null}
            <p className="project-id text-[9px] text-muted-foreground">
              profile {detail.screening.profile_digest.slice(0, 16)} · {detail.screening.model_provider}/
              {detail.screening.model} · {formatProjectTime(detail.screening.screened_at)}
            </p>
            {detail.screening.screening_trigger === 'operator_rescreen' ? (
              <div className="rounded-md border border-blue-200 bg-blue-50 px-3 py-2 text-[10px] leading-4 text-blue-800">
                <p>
                  Re-screen requested by {detail.screening.requested_by ?? 'operator'}:{' '}
                  {detail.screening.request_reason}
                </p>
                {detail.screening.probe_evidence.length ? (
                  <ul className="mt-1 list-disc space-y-1 pl-4">
                    {detail.screening.probe_evidence.map(value => (
                      <li key={value}>{value}</li>
                    ))}
                  </ul>
                ) : null}
              </div>
            ) : null}
            {detail.screening_history.length ? (
              <details className="rounded-md border border-border bg-muted/20 px-3 py-2">
                <summary className="cursor-pointer text-[10px] font-semibold text-muted-foreground">
                  Screening history · {detail.screening_history.length} immutable revision
                  {detail.screening_history.length === 1 ? '' : 's'}
                </summary>
                <ol className="mt-3 space-y-3">
                  {detail.screening_history
                    .slice()
                    .reverse()
                    .map(revision => (
                      <li
                        key={`${revision.candidate_snapshot_digest}-${revision.revision}`}
                        className="border-l-2 border-blue-200 pl-3 text-[10px] leading-4"
                      >
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="project-data font-semibold">rev {revision.revision}</span>
                          <span className="project-data">{revision.decision}</span>
                          <span className="text-muted-foreground">{formatProjectTime(revision.screened_at)}</span>
                        </div>
                        <p className="mt-1">{revision.reason}</p>
                        {revision.request_reason ? (
                          <p className="mt-1 text-muted-foreground">Request: {revision.request_reason}</p>
                        ) : null}
                      </li>
                    ))}
                </ol>
              </details>
            ) : null}
            {!work ? (
              <div className="rounded-md border border-border bg-card px-3 py-3">
                <p className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                  Append verified re-screen
                </p>
                <p className="mt-1 text-[10px] leading-4 text-muted-foreground">
                  Supply the audit reason and bounded evidence produced by a real probe. This creates a new immutable
                  screening revision; it does not approve the Issue for Work.
                </p>
                <label className="mt-3 block text-[10px] font-medium">
                  Re-screen reason
                  <textarea
                    value={rescreenReason}
                    onChange={event => onRescreenReasonChange(event.target.value)}
                    maxLength={1000}
                    rows={2}
                    placeholder="What uncertainty did the probe resolve?"
                    className="mt-1 w-full resize-y rounded-md border border-border bg-card px-2.5 py-2 text-[11px] leading-4 outline-none transition placeholder:text-muted-foreground focus:border-blue-500"
                  />
                </label>
                <label className="mt-2 block text-[10px] font-medium">
                  Probe evidence · one fact per line
                  <textarea
                    value={probeEvidence}
                    onChange={event => onProbeEvidenceChange(event.target.value)}
                    rows={3}
                    placeholder="Command context and observed result, without credentials"
                    className="mt-1 w-full resize-y rounded-md border border-border bg-card px-2.5 py-2 text-[11px] leading-4 outline-none transition placeholder:text-muted-foreground focus:border-blue-500"
                  />
                </label>
                <Button
                  type="button"
                  onClick={onRescreen}
                  disabled={rescreening || !rescreenReason.trim() || !probeEvidence.trim()}
                  className="mt-2"
                >
                  {rescreening ? <Spinner /> : <RefreshCw className="h-3.5 w-3.5" />}
                  Run screening Subagent again
                </Button>
              </div>
            ) : null}
          </div>
        ) : (
          <p className="mt-2 text-xs leading-5 text-muted-foreground">
            This frozen Issue snapshot is waiting for its complete SELECT / DEFER / REJECT decision.
          </p>
        )}
      </section>

      {detail.operator_selection_required ? (
        <section className="px-5 py-4">
          <h3 className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            <UserCheck className="h-3.5 w-3.5" /> Operator admission
          </h3>
          {detail.operator_selected ? (
            <p className="mt-2 text-xs leading-5 text-green-700">
              You approved this Issue. Main Hermes now owns the normal Plan → Codex Worker → Review flow.
            </p>
          ) : detail.operator_selectable ? (
            <div className="mt-3 flex flex-col items-start gap-2">
              <p className="text-xs leading-5 text-muted-foreground">
                The screening Subagent marked this Issue SELECT, but scanning still cannot start Work. Confirm it here
                after reviewing the reason and machine requirements.
              </p>
              <Button type="button" onClick={onSelectForWork} disabled={selecting}>
                {selecting ? <Spinner /> : <UserCheck className="h-3.5 w-3.5" />}
                Approve for full workflow
              </Button>
            </div>
          ) : (
            <p className="mt-2 text-xs leading-5 text-muted-foreground">
              This Issue needs a current Subagent SELECT decision before operator admission.
            </p>
          )}
        </section>
      ) : null}

      <section className="px-5 py-4">
        <h3 className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          <Tag className="h-3.5 w-3.5" /> Issue evidence
        </h3>
        <p className="mt-3 max-h-64 overflow-y-auto whitespace-pre-wrap text-xs leading-5 text-card-foreground/90">
          {candidate.body || 'This Issue has no body.'}
        </p>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {candidate.labels.map(label => (
            <span key={label} className="rounded-full bg-violet-50 px-2 py-0.5 text-[9px] font-medium text-violet-700">
              {label}
            </span>
          ))}
        </div>
      </section>

      <section className="grid gap-4 px-5 py-4 md:grid-cols-2">
        <div>
          <h3 className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            <Filter className="h-3.5 w-3.5" /> Filter result
          </h3>
          {candidate.eligible ? (
            <p className="mt-2 flex items-start gap-2 text-xs leading-5 text-green-700">
              <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              {detail.operator_selection_required && !detail.operator_selected
                ? 'Passed the mechanical filter and is waiting for operator selection.'
                : 'Passed the mechanical filter and entered the Work flow.'}
            </p>
          ) : (
            <ul className="mt-2 list-disc space-y-1 pl-4 text-xs leading-5 text-muted-foreground">
              {candidate.filter_reasons.map(reason => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          )}
        </div>
        <div>
          <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Current step</h3>
          <p className="mt-2 text-xs leading-5">{work?.current_step ?? 'Not admitted to Work.'}</p>
          {work?.blocked_reason ? <p className="mt-2 text-xs text-orange-700">{work.blocked_reason}</p> : null}
          {work?.last_error ? <p className="mt-2 text-xs text-red-700">{work.last_error}</p> : null}
        </div>
      </section>

      {work?.plan ? (
        <section className="px-5 py-4">
          <h3 className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            <GitPullRequest className="h-3.5 w-3.5" /> Main Hermes plan
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
        </section>
      ) : null}

      {work?.environment_status === 'verified' && work.resource_requirements ? (
        <section className="px-5 py-4">
          <h3 className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            <ServerCog className="h-3.5 w-3.5" /> Verified resources
          </h3>
          <dl className="mt-3 grid grid-cols-2 gap-3 text-[10px] sm:grid-cols-4">
            <div>
              <dt className="text-muted-foreground">CPU</dt>
              <dd className="project-data mt-1 font-semibold">
                {work.resource_requirements.cpu_request} / {work.resource_requirements.cpu_limit}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Memory</dt>
              <dd className="project-data mt-1 font-semibold">
                {work.resource_requirements.memory_request} / {work.resource_requirements.memory_limit}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">GPU</dt>
              <dd className="project-data mt-1 font-semibold">
                {work.resource_requirements.gpu_count} · {work.resource_requirements.gpu_architecture ?? 'none'}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Worker model</dt>
              <dd className="project-data mt-1 break-all font-semibold">{work.resource_requirements.worker_model}</dd>
            </div>
          </dl>
          <p className="project-id mt-3 text-[9px] text-muted-foreground">baseline {work.named_baseline}</p>
        </section>
      ) : null}

      {events.length ? (
        <section className="px-5 py-4">
          <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            Recent Work events
          </h3>
          <ol className="mt-3 space-y-2">
            {events
              .slice(-8)
              .reverse()
              .map(event => (
                <li key={event.event_id} className="flex items-center justify-between gap-4 text-[10px]">
                  <span className="project-id font-medium">{event.event_type}</span>
                  <time className="project-data shrink-0 text-[9px] text-muted-foreground">
                    {formatProjectTime(event.created_at)}
                  </time>
                </li>
              ))}
          </ol>
        </section>
      ) : null}
    </div>
  )
}

export default function IssuesPage() {
  const { setEnd } = usePageHeader()
  const [overview, setOverview] = useState<PollingOverviewResponse | null>(null)
  const [feed, setFeed] = useState<ProjectIssueListResponse | null>(null)
  const [detail, setDetail] = useState<ProjectIssueDetailResponse | null>(null)
  const [repositoryId, setRepositoryId] = useState<number | null>(null)
  const [eligibility, setEligibility] = useState<EligibilityFilter>('all')
  const [screening, setScreening] = useState<ScreeningFilter>('SELECT')
  const [status, setStatus] = useState<StatusFilter>('all')
  const [search, setSearch] = useState('')
  const deferredSearch = useDeferredValue(search)
  const [page, setPage] = useState(0)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [selecting, setSelecting] = useState(false)
  const [rescreening, setRescreening] = useState(false)
  const [rescreenDraft, setRescreenDraft] = useState({
    candidateId: null as string | null,
    reason: '',
    probeEvidence: ''
  })
  const [error, setError] = useState<string | null>(null)

  const refreshFeed = useCallback(
    async (background = false) => {
      if (background) setRefreshing(true)
      else setLoading(true)
      try {
        const [nextFeed, nextOverview] = await Promise.all([
          api.listProjectIssues({
            repositoryId: repositoryId ?? undefined,
            eligible: eligibility === 'all' ? undefined : eligibility === 'matched',
            search: deferredSearch,
            status: status === 'all' ? undefined : [status],
            screeningDecision: screening === 'all' ? undefined : [screening],
            limit: PAGE_SIZE,
            offset: page * PAGE_SIZE
          }),
          api.getPollingOverview()
        ])
        setFeed(nextFeed)
        setOverview(nextOverview)
        setSelectedId(current => {
          if (current && nextFeed.issues.some(item => item.candidate.candidate_id === current)) return current
          return nextFeed.issues[0]?.candidate.candidate_id ?? null
        })
        setError(null)
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : String(reason))
      } finally {
        setLoading(false)
        setRefreshing(false)
      }
    },
    [deferredSearch, eligibility, page, repositoryId, screening, status]
  )

  useEffect(() => {
    void refreshFeed()
    const timer = window.setInterval(() => void refreshFeed(true), REFRESH_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [refreshFeed])

  useEffect(() => {
    if (!selectedId) {
      setDetail(null)
      return
    }
    let active = true
    setDetailLoading(true)
    api
      .getProjectIssue(selectedId)
      .then(next => {
        if (active) setDetail(next)
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : String(reason))
      })
      .finally(() => {
        if (active) setDetailLoading(false)
      })
    return () => {
      active = false
    }
  }, [selectedId, feed])

  useEffect(() => {
    setPage(0)
  }, [deferredSearch, eligibility, repositoryId, screening, status])

  useEffect(() => {
    setEnd(
      <Button
        ghost
        size="icon"
        type="button"
        onClick={() => void refreshFeed(true)}
        disabled={loading || refreshing}
        aria-label="Refresh live Issue feed"
      >
        {loading || refreshing ? <Spinner /> : <RefreshCw />}
      </Button>
    )
    return () => setEnd(null)
  }, [loading, refreshFeed, refreshing, setEnd])

  const totalPages = Math.max(1, Math.ceil((feed?.total ?? 0) / PAGE_SIZE))
  const selectedItem = useMemo(
    () => feed?.issues.find(item => item.candidate.candidate_id === selectedId) ?? null,
    [feed, selectedId]
  )
  const activeRescreenReason = rescreenDraft.candidateId === selectedId ? rescreenDraft.reason : ''
  const activeProbeEvidence = rescreenDraft.candidateId === selectedId ? rescreenDraft.probeEvidence : ''

  const selectForWork = useCallback(async () => {
    if (!detail?.operator_selectable) return
    const approved = window.confirm(
      `Approve ${detail.candidate.repository} #${detail.candidate.issue_number} for Main Hermes planning and Codex execution?`
    )
    if (!approved) return
    setSelecting(true)
    try {
      const next = await api.selectProjectIssue(detail.candidate.candidate_id)
      setDetail(next)
      await refreshFeed(true)
      setError(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setSelecting(false)
    }
  }, [detail, refreshFeed])

  const rescreenIssue = useCallback(async () => {
    if (!detail?.screening || detail.work_item) return
    const reason = activeRescreenReason.trim()
    const evidence = activeProbeEvidence
      .split('\n')
      .map(value => value.trim())
      .filter(Boolean)
    if (!reason || !evidence.length) return
    setRescreening(true)
    try {
      const next = await api.rescreenProjectIssue(detail.candidate.candidate_id, {
        reason,
        probe_evidence: evidence
      })
      setDetail(next)
      setRescreenDraft({ candidateId: null, reason: '', probeEvidence: '' })
      await refreshFeed(true)
      setError(null)
    } catch (rescreenError) {
      setError(rescreenError instanceof Error ? rescreenError.message : String(rescreenError))
    } finally {
      setRescreening(false)
    }
  }, [activeProbeEvidence, activeRescreenReason, detail, refreshFeed])

  return (
    <div className="project-hermes-ui space-y-4 py-4">
      <ProjectPanel bodyClassName="px-4 py-3">
        <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
          <div>
            <p className="text-[10px] font-semibold uppercase tracking-[0.1em] text-blue-600">
              Live Issue intelligence
            </p>
            <h2 className="mt-1 text-lg font-semibold">Issue feed</h2>
            <p className="mt-1 text-[11px] text-muted-foreground">
              Continuous 30-day repository scans · refreshed every 5 seconds
            </p>
            {overview?.operator_selection_required ? (
              <p className="mt-2 text-[10px] font-medium text-orange-700">
                Guided training mode: the generic Subagent screens every snapshot; only your explicit confirmation
                starts the normal workflow.
              </p>
            ) : null}
          </div>
          <RepositorySelect
            repositories={overview?.repository_stats ?? []}
            value={repositoryId}
            onChange={setRepositoryId}
          />
        </div>
        <div className="mt-3 flex flex-col gap-2 border-t border-border pt-3 lg:flex-row lg:items-center">
          <label className="relative min-w-0 flex-1">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <input
              value={search}
              onChange={event => setSearch(event.target.value)}
              placeholder="Search repository, title, or Issue number"
              className="w-full rounded-md border border-border bg-card py-1.5 pl-8 pr-3 text-[11px] outline-none transition placeholder:text-muted-foreground focus:border-blue-500"
            />
          </label>
          <div className="flex flex-wrap gap-1">
            {(['SELECT', 'DEFER', 'REJECT', 'PENDING', 'all'] as ScreeningFilter[]).map(value => (
              <button
                key={value}
                type="button"
                onClick={() => setScreening(value)}
                className={cn(
                  'rounded-full border px-2.5 py-1 text-[9px] font-semibold uppercase transition',
                  screening === value
                    ? 'border-green-600 bg-green-600 text-white'
                    : 'border-border bg-card text-muted-foreground hover:border-green-600 hover:text-green-700'
                )}
              >
                {value}
              </button>
            ))}
          </div>
          <div className="flex flex-wrap gap-1">
            {(['matched', 'filtered', 'all'] as EligibilityFilter[]).map(value => (
              <button
                key={value}
                type="button"
                onClick={() => setEligibility(value)}
                className={cn(
                  'rounded-full border px-2.5 py-1 text-[9px] font-semibold uppercase transition',
                  eligibility === value
                    ? 'border-blue-500 bg-blue-500 text-white'
                    : 'border-border bg-card text-muted-foreground hover:border-blue-500 hover:text-blue-600'
                )}
              >
                {value}
              </button>
            ))}
          </div>
          <select
            value={status}
            onChange={event => setStatus(event.target.value as StatusFilter)}
            className="project-data rounded-md border border-border bg-card px-2.5 py-1.5 text-[10px] outline-none focus:border-blue-500"
          >
            {STATUS_FILTERS.map(value => (
              <option key={value} value={value}>
                status: {value}
              </option>
            ))}
          </select>
        </div>
      </ProjectPanel>

      {error ? (
        <div role="alert" className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700">
          {error}
        </div>
      ) : null}

      <section className="grid gap-4 xl:grid-cols-[minmax(21rem,0.9fr)_minmax(28rem,1.1fr)]">
        <ProjectPanel
          title={`Issues · ${feed?.total ?? 0}`}
          subtitle={
            selectedItem
              ? `${selectedItem.candidate.repository} #${selectedItem.candidate.issue_number} selected`
              : 'Waiting for a matching Issue'
          }
          action={<span className="project-data text-[9px] text-green-600">● LIVE</span>}
        >
          <div className="max-h-[calc(100dvh-20rem)] min-h-[30rem] overflow-y-auto">
            {loading && !feed ? (
              <div className="flex min-h-[30rem] items-center justify-center">
                <Spinner />
              </div>
            ) : feed?.issues.length ? (
              feed.issues.map(item => (
                <IssueFeedRow
                  key={item.candidate.candidate_id}
                  item={item}
                  selected={item.candidate.candidate_id === selectedId}
                  onSelect={() => setSelectedId(item.candidate.candidate_id)}
                />
              ))
            ) : (
              <div className="flex min-h-[30rem] flex-col items-center justify-center px-6 text-center text-xs text-muted-foreground">
                <Filter className="mb-3 h-6 w-6 opacity-50" />
                No Issues match the current filters.
              </div>
            )}
          </div>
          <footer className="flex items-center justify-between border-t border-border px-4 py-2.5">
            <Button
              ghost
              size="icon"
              type="button"
              disabled={page === 0}
              onClick={() => setPage(value => Math.max(0, value - 1))}
            >
              <ChevronLeft />
            </Button>
            <span className="project-data text-[9px] text-muted-foreground">
              {page + 1} / {totalPages}
            </span>
            <Button
              ghost
              size="icon"
              type="button"
              disabled={page + 1 >= totalPages}
              onClick={() => setPage(value => value + 1)}
            >
              <ChevronRight />
            </Button>
          </footer>
        </ProjectPanel>

        <ProjectPanel title="Issue detail" subtitle="Evidence, Main Hermes plan, and verified resources">
          <div className="max-h-[calc(100dvh-15rem)] overflow-y-auto">
            <IssueDetail
              detail={detail}
              loading={detailLoading}
              selecting={selecting}
              rescreening={rescreening}
              rescreenReason={activeRescreenReason}
              probeEvidence={activeProbeEvidence}
              onRescreenReasonChange={value =>
                setRescreenDraft(current => ({
                  candidateId: selectedId,
                  reason: value,
                  probeEvidence: current.candidateId === selectedId ? current.probeEvidence : ''
                }))
              }
              onProbeEvidenceChange={value =>
                setRescreenDraft(current => ({
                  candidateId: selectedId,
                  reason: current.candidateId === selectedId ? current.reason : '',
                  probeEvidence: value
                }))
              }
              onRescreen={() => void rescreenIssue()}
              onSelectForWork={() => void selectForWork()}
            />
          </div>
        </ProjectPanel>
      </section>
    </div>
  )
}
