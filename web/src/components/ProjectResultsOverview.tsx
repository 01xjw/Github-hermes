import { useState } from 'react'
import { Link } from 'react-router'

import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  GitPullRequest,
  ScanSearch,
  ShieldCheck,
  TriangleAlert
} from 'lucide-react'

import { ProjectPanel, formatProjectTime } from '@/components/ProjectHermesUi'
import type { PollingOverviewResponse, PollingRepositoryStat, ProjectWorkStatus } from '@/lib/api'
import { PROJECT_WORK_STATUS_ORDER, buildProjectOutcomeSummary } from '@/lib/project-outcomes'
import { cn } from '@/lib/utils'

const STATUS_PRESENTATION: Record<ProjectWorkStatus, { label: string; bar: string; badge: string }> = {
  queued: {
    label: 'Queued',
    bar: 'bg-slate-400',
    badge: 'border-slate-200 bg-slate-50 text-slate-700'
  },
  planning: {
    label: 'Planning',
    bar: 'bg-violet-500',
    badge: 'border-violet-200 bg-violet-50 text-violet-700'
  },
  running: {
    label: 'Running',
    bar: 'bg-blue-500',
    badge: 'border-blue-200 bg-blue-50 text-blue-700'
  },
  review: {
    label: 'Review',
    bar: 'bg-amber-500',
    badge: 'border-amber-200 bg-amber-50 text-amber-700'
  },
  done: {
    label: 'Done',
    bar: 'bg-emerald-500',
    badge: 'border-emerald-200 bg-emerald-50 text-emerald-700'
  },
  blocked: {
    label: 'Blocked',
    bar: 'bg-orange-500',
    badge: 'border-orange-200 bg-orange-50 text-orange-700'
  },
  failed: {
    label: 'Failed',
    bar: 'bg-rose-500',
    badge: 'border-rose-200 bg-rose-50 text-rose-700'
  }
}

function PipelineStep({ label, value, detail }: { label: string; value: number; detail: string }) {
  return (
    <div className="min-w-0 flex-1 rounded-md border border-border bg-muted/10 px-3 py-3">
      <p className="text-[9px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">{label}</p>
      <p className="project-data mt-1.5 text-xl font-bold">{value}</p>
      <p className="mt-1 truncate text-[9px] text-muted-foreground" title={detail}>
        {detail}
      </p>
    </div>
  )
}

export function ProjectPipelineResults({ overview }: { overview: PollingOverviewResponse }) {
  const summary = buildProjectOutcomeSummary(overview)
  const statusTotal = Math.max(1, summary.workItems)

  const pipeline = [
    {
      label: 'Retained Issues',
      value: overview.candidate_counts.total,
      detail: `${overview.candidate_counts.matched} mechanically eligible`
    },
    {
      label: 'Screened',
      value: summary.screened,
      detail: `${overview.screening_counts.PENDING} awaiting decision`
    },
    {
      label: 'Selected',
      value: summary.selected,
      detail: `${summary.selectionRate}% of completed screenings`
    },
    {
      label: 'Work admitted',
      value: summary.workItems,
      detail: `${summary.active} currently active or queued`
    },
    {
      label: 'Delivered',
      value: summary.delivered,
      detail: 'Independent review completed'
    }
  ]

  return (
    <ProjectPanel
      title="Pipeline results"
      subtitle="Durable outcome funnel from retained Issue to independently reviewed delivery"
      action={
        <Link to="/work" className="text-[10px] font-semibold text-blue-600 hover:underline">
          Inspect Work
        </Link>
      }
      bodyClassName="space-y-4 p-4"
    >
      <div className="flex flex-col gap-2 lg:flex-row lg:items-stretch">
        {pipeline.map((step, index) => (
          <div key={step.label} className="contents">
            <PipelineStep {...step} />
            {index < pipeline.length - 1 ? (
              <span className="hidden items-center text-muted-foreground/60 lg:flex" aria-hidden="true">
                <ChevronRight className="h-4 w-4" />
              </span>
            ) : null}
          </div>
        ))}
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.4fr)_minmax(17rem,0.6fr)]">
        <section className="rounded-md border border-border px-3 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h4 className="text-[10px] font-semibold uppercase tracking-[0.08em]">Work outcome distribution</h4>
              <p className="mt-1 text-[9px] text-muted-foreground">
                {summary.workItems} admitted Work items across every lifecycle state
              </p>
            </div>
            <span className="project-data text-[10px] font-semibold text-emerald-700">
              {summary.terminalSuccessRate}% terminal success
            </span>
          </div>
          <div
            className="mt-3 flex h-2.5 overflow-hidden rounded-full bg-muted"
            role="img"
            aria-label="Work outcome distribution"
          >
            {PROJECT_WORK_STATUS_ORDER.map(status => {
              const value = overview.work_counts[status]
              if (!value) return null
              return (
                <span
                  key={status}
                  className={STATUS_PRESENTATION[status].bar}
                  style={{ width: `${(value / statusTotal) * 100}%` }}
                  title={`${STATUS_PRESENTATION[status].label}: ${value}`}
                />
              )
            })}
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            {PROJECT_WORK_STATUS_ORDER.map(status => (
              <span
                key={status}
                className={cn(
                  'project-data inline-flex items-center gap-1.5 rounded-full border px-2 py-1 text-[9px] font-semibold',
                  STATUS_PRESENTATION[status].badge
                )}
              >
                <span className={cn('h-1.5 w-1.5 rounded-full', STATUS_PRESENTATION[status].bar)} />
                {STATUS_PRESENTATION[status].label} {overview.work_counts[status]}
              </span>
            ))}
          </div>
        </section>

        <section className="grid grid-cols-2 gap-2">
          <div className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-emerald-800">
            <CheckCircle2 className="h-4 w-4" />
            <p className="project-data mt-2 text-xl font-bold">{summary.delivered}</p>
            <p className="mt-1 text-[9px] font-semibold uppercase tracking-[0.08em]">Done</p>
          </div>
          <div className="rounded-md border border-orange-200 bg-orange-50 p-3 text-orange-800">
            <TriangleAlert className="h-4 w-4" />
            <p className="project-data mt-2 text-xl font-bold">{summary.needsAttention}</p>
            <p className="mt-1 text-[9px] font-semibold uppercase tracking-[0.08em]">Blocked / failed</p>
          </div>
          <div className="rounded-md border border-blue-200 bg-blue-50 p-3 text-blue-800">
            <ShieldCheck className="h-4 w-4" />
            <p className="mt-2 text-[10px] font-semibold">
              {overview.operator_selection_required ? 'Manual admission' : 'Automatic admission'}
            </p>
            <p className="mt-1 text-[9px] leading-4 opacity-80">Machine gates remain enforced</p>
          </div>
          <div className="rounded-md border border-violet-200 bg-violet-50 p-3 text-violet-800">
            <GitPullRequest className="h-4 w-4" />
            <p className="mt-2 text-[10px] font-semibold">Two-stage review</p>
            <p className="mt-1 text-[9px] leading-4 opacity-80">Done means both reviewers approved</p>
          </div>
        </section>
      </div>
    </ProjectPanel>
  )
}

function formatCoverageDate(value: string | null | undefined): string {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC'
  }).format(parsed)
}

const SCAN_TONES: Record<string, string> = {
  completed: 'border-emerald-200 bg-emerald-50 text-emerald-700',
  partial: 'border-amber-200 bg-amber-50 text-amber-700',
  running: 'border-blue-200 bg-blue-50 text-blue-700',
  failed: 'border-rose-200 bg-rose-50 text-rose-700'
}

export function RepositoryCoverageTable({
  repositories,
  onSelect
}: {
  repositories: PollingRepositoryStat[]
  onSelect: (repositoryId: number) => void
}) {
  const [expanded, setExpanded] = useState(false)

  return (
    <ProjectPanel
      title={`Repository coverage · ${repositories.length}`}
      action={
        <button
          type="button"
          onClick={() => setExpanded(current => !current)}
          aria-expanded={expanded}
          className="inline-flex items-center gap-1.5 text-[10px] font-semibold text-muted-foreground transition hover:text-blue-600"
        >
          {expanded ? 'Collapse' : 'Expand'}
          <ChevronDown className={cn('h-3.5 w-3.5 transition-transform', expanded && 'rotate-180')} />
        </button>
      }
    >
      {expanded ? (
        <>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1020px] text-left text-[10px]">
              <thead className="border-b border-border bg-muted/30 text-[9px] uppercase tracking-[0.08em] text-muted-foreground">
                <tr>
                  <th className="px-4 py-2.5 font-semibold">Repository</th>
                  <th className="px-3 py-2.5 font-semibold">Issue window</th>
                  <th className="px-3 py-2.5 font-semibold">Batch state</th>
                  <th className="px-3 py-2.5 text-right font-semibold">Seen</th>
                  <th className="px-3 py-2.5 text-right font-semibold">Retained</th>
                  <th className="px-3 py-2.5 text-right font-semibold">Selected</th>
                  <th className="px-3 py-2.5 text-right font-semibold">Work</th>
                  <th className="px-3 py-2.5 text-right font-semibold">Done</th>
                  <th className="px-4 py-2.5 text-right font-semibold">Updated</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {repositories.map(repository => {
                  const scan = repository.coverage_scan
                  const latestStatus = repository.latest_scan?.status
                  const results = repository.coverage_results
                  return (
                    <tr key={repository.repository_id} className="transition hover:bg-muted/30">
                      <td className="px-4 py-3">
                        <button
                          type="button"
                          onClick={() => onSelect(repository.repository_id)}
                          className="font-semibold hover:text-blue-600 hover:underline"
                        >
                          {repository.repository}
                        </button>
                      </td>
                      <td className="project-data whitespace-nowrap px-3 py-3 text-[9px]">
                        {scan
                          ? `${formatCoverageDate(scan.cutoff)} → ${formatCoverageDate(scan.window_end)}`
                          : 'Not scanned'}
                      </td>
                      <td className="px-3 py-3">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <span
                            className={cn(
                              'project-data rounded-full border px-2 py-0.5 text-[9px] font-semibold uppercase',
                              SCAN_TONES[scan?.status ?? ''] ?? 'border-slate-200 bg-slate-50 text-slate-600'
                            )}
                          >
                            {scan?.status ?? 'pending'}
                          </span>
                          {scan ? (
                            <span className="project-data rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[9px] font-semibold uppercase text-slate-600">
                              {scan.scan_mode}
                            </span>
                          ) : null}
                          {repository.coverage_stale ? (
                            <span
                              title={repository.latest_scan?.error ?? undefined}
                              className="project-data inline-flex items-center gap-1 rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[9px] font-semibold uppercase text-amber-700"
                            >
                              <TriangleAlert className="h-3 w-3" />
                              stale · latest {latestStatus ?? 'unavailable'}
                            </span>
                          ) : null}
                        </div>
                      </td>
                      <td className="project-data px-3 py-3 text-right font-semibold">{results.seen}</td>
                      <td
                        className="project-data px-3 py-3 text-right font-semibold"
                        title={`${results.matched} matched · ${results.filtered} filtered`}
                      >
                        {results.retained}
                      </td>
                      <td className="project-data px-3 py-3 text-right font-semibold text-teal-700">
                        {results.selected}
                      </td>
                      <td className="project-data px-3 py-3 text-right font-semibold text-blue-700">{results.work}</td>
                      <td className="project-data px-3 py-3 text-right font-semibold text-emerald-700">
                        {results.done}
                      </td>
                      <td className="project-data whitespace-nowrap px-4 py-3 text-right text-[9px] text-muted-foreground">
                        {formatProjectTime(repository.results_updated_at)}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          <div className="flex items-start gap-2 border-t border-border bg-muted/20 px-4 py-3 text-[9px] leading-4 text-muted-foreground">
            <ScanSearch className="mt-0.5 h-3 w-3 shrink-0" />
            <p>
              Every row uses its repository's latest exact fresh or backfill window. Downstream Selected, Work, and
              Done values refresh from current durable state; stale rows retain the last valid batch.
            </p>
          </div>
        </>
      ) : null}
    </ProjectPanel>
  )
}
