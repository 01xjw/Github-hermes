import { useCallback, useEffect, useRef, useState } from 'react'
import { CheckCircle2, ChevronLeft, ChevronRight, Clock3, ExternalLink, RefreshCw } from 'lucide-react'
import { Button } from '@nous-research/ui/ui/components/button'
import { Spinner } from '@nous-research/ui/ui/components/spinner'

import { ProjectPanel, WorkStatusBadge, formatProjectTime } from '@/components/ProjectHermesUi'
import { api } from '@/lib/api'
import type { ProjectWorkItem, ProjectWorkListResponse } from '@/lib/api'
import { cn } from '@/lib/utils'

const PAGE_SIZE = 25
const REFRESH_INTERVAL_MS = 10_000

interface CompletedSolutionsTableProps {
  onSelect?: (item: ProjectWorkItem) => void
}

export function CompletedSolutionsTable({ onSelect }: CompletedSolutionsTableProps) {
  const [page, setPage] = useState(0)
  const [solutions, setSolutions] = useState<ProjectWorkListResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inFlight = useRef(false)

  const refresh = useCallback(
    async (background = false) => {
      if (inFlight.current) return
      inFlight.current = true
      if (background) setRefreshing(true)
      else setLoading(true)
      try {
        const next = await api.listProjectWorkItems({
          status: ['done'],
          limit: PAGE_SIZE,
          offset: page * PAGE_SIZE
        })
        setSolutions(next)
        const lastPage = Math.max(0, Math.ceil(next.counts.done / PAGE_SIZE) - 1)
        if (page > lastPage) setPage(lastPage)
        setError(null)
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : String(reason))
      } finally {
        setLoading(false)
        setRefreshing(false)
        inFlight.current = false
      }
    },
    [page]
  )

  useEffect(() => {
    let active = true
    const refreshWhenVisible = () => {
      if (active && document.visibilityState === 'visible') {
        void refresh(true)
      }
    }

    queueMicrotask(() => {
      if (active) void refresh()
    })
    const timer = window.setInterval(refreshWhenVisible, REFRESH_INTERVAL_MS)
    window.addEventListener('focus', refreshWhenVisible)
    document.addEventListener('visibilitychange', refreshWhenVisible)
    return () => {
      active = false
      window.clearInterval(timer)
      window.removeEventListener('focus', refreshWhenVisible)
      document.removeEventListener('visibilitychange', refreshWhenVisible)
    }
  }, [refresh])

  const total = solutions?.counts.done ?? 0
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const firstItem = total ? page * PAGE_SIZE + 1 : 0
  const lastItem = Math.min((page + 1) * PAGE_SIZE, total)

  return (
    <ProjectPanel
      title={`Completed solutions · ${total}`}
      subtitle="Every Work item that completed both independent review stages"
      action={
        <button
          type="button"
          onClick={() => void refresh(true)}
          disabled={loading || refreshing}
          className="inline-flex items-center gap-1 text-[10px] font-semibold text-muted-foreground transition hover:text-blue-600 disabled:opacity-50"
          aria-label="Refresh completed solutions"
        >
          <RefreshCw className={cn('h-3 w-3', (loading || refreshing) && 'animate-spin')} />
          Live
        </button>
      }
    >
      {error ? (
        <div role="alert" className="border-b border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700">
          {error}
        </div>
      ) : null}
      <div className="overflow-x-auto">
        {loading && !solutions ? (
          <div className="flex min-h-48 items-center justify-center">
            <Spinner />
          </div>
        ) : (
          <table className="w-full min-w-[900px] text-left text-[11px]">
            <thead className="border-b border-border bg-muted/30 text-[9px] uppercase tracking-[0.08em] text-muted-foreground">
              <tr>
                <th className="px-4 py-2.5 font-semibold">Repository / Issue</th>
                <th className="px-3 py-2.5 font-semibold">Completed solution</th>
                <th className="px-3 py-2.5 font-semibold">Final state</th>
                <th className="px-3 py-2.5 font-semibold">Evidence identity</th>
                <th className="px-4 py-2.5 text-right font-semibold">Completed</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {solutions?.work_items.length ? (
                solutions.work_items.map(item => (
                  <tr
                    key={item.work_item_id}
                    data-completed-solution={item.work_item_id}
                    onClick={() => onSelect?.(item)}
                    className={cn('transition hover:bg-muted/30', onSelect && 'cursor-pointer')}
                  >
                    <td className="px-4 py-3 align-top">
                      <p className="font-semibold">{item.repository}</p>
                      <a
                        href={item.issue_url}
                        target="_blank"
                        rel="noreferrer"
                        onClick={event => event.stopPropagation()}
                        className="project-id mt-1 inline-flex items-center gap-1 font-semibold text-blue-600 hover:underline"
                      >
                        Issue #{item.issue_number}
                        <ExternalLink className="h-3 w-3" />
                      </a>
                    </td>
                    <td className="max-w-[34rem] px-3 py-3 align-top">
                      <p className="font-semibold">{item.title}</p>
                      <p className="mt-1 line-clamp-2 leading-5 text-muted-foreground">
                        {item.plan?.summary ?? item.current_step}
                      </p>
                    </td>
                    <td className="px-3 py-3 align-top">
                      <WorkStatusBadge status={item.status} />
                      <p className="mt-2 inline-flex items-center gap-1 text-[9px] text-muted-foreground">
                        <CheckCircle2 className="h-3 w-3 text-emerald-600" />
                        Reviews approved
                      </p>
                    </td>
                    <td className="px-3 py-3 align-top">
                      <p className="project-id max-w-52 truncate text-[9px]">
                        {item.internal_candidate_id ?? item.work_item_id}
                      </p>
                      <p className="project-data mt-1 text-[9px] text-muted-foreground">
                        attempt {item.execution_attempt || 1}
                      </p>
                    </td>
                    <td className="project-data whitespace-nowrap px-4 py-3 text-right text-[9px] text-muted-foreground">
                      {formatProjectTime(item.completed_at ?? item.updated_at)}
                    </td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={5} className="px-4 py-14 text-center text-xs text-muted-foreground">
                    <Clock3 className="mx-auto mb-3 h-6 w-6 opacity-50" />
                    No completed Work has been recorded yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>
      <footer className="flex items-center justify-between border-t border-border px-4 py-2.5">
        <Button
          ghost
          size="icon"
          type="button"
          disabled={page === 0 || loading}
          onClick={() => setPage(value => Math.max(0, value - 1))}
          aria-label="Previous completed solutions page"
        >
          <ChevronLeft />
        </Button>
        <span className="project-data text-[9px] text-muted-foreground">
          {firstItem}–{lastItem} of {total} · page {page + 1} / {totalPages}
        </span>
        <Button
          ghost
          size="icon"
          type="button"
          disabled={page + 1 >= totalPages || loading}
          onClick={() => setPage(value => value + 1)}
          aria-label="Next completed solutions page"
        >
          <ChevronRight />
        </Button>
      </footer>
    </ProjectPanel>
  )
}
