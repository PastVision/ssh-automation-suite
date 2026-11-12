/**
 * Execution history page.
 *
 * Lists runs newest-first and expands one into its per-host, per-step breakdown.
 * A host row opens the filesystem transcript, which is the only place raw output
 * is shown -- the list and the detail carry metadata only.
 *
 * The list polls while something is still running, so a launch started from the
 * Workflows page becomes visible here without a manual refresh.
 */

import { CalendarClock, ListChecks, RefreshCw } from 'lucide-react'
import { useState } from 'react'

import { StatusBadge } from '@/components/executions/status-badge'
import { TranscriptDialog } from '@/components/executions/transcript-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { useExecution, useExecutions } from '@/hooks/use-executions'
import { formatApiInstant, parseApiInstant } from '@/lib/display'
import type { ExecutionStatus, HostRun } from '@/lib/types'

const FILTERS: Array<ExecutionStatus | 'all'> = [
  'all',
  'running',
  'succeeded',
  'failed',
  'cancelled',
  'pending',
]

function formatDate(value: string | null): string {
  return formatApiInstant(value)
}

function formatDuration(start: string | null, end: string | null): string {
  const from = parseApiInstant(start)
  const to = parseApiInstant(end)
  if (!from || !to) return '—'
  const ms = to.getTime() - from.getTime()
  if (!Number.isFinite(ms) || ms < 0) return '—'
  if (ms < 1000) return `${ms} ms`
  const seconds = ms / 1000
  return seconds < 60 ? `${seconds.toFixed(1)} s` : `${Math.round(seconds / 60)} min`
}

function HostBreakdown({
  executionId,
  onOpenTranscript,
}: {
  executionId: number
  onOpenTranscript: (host: HostRun) => void
}) {
  const detail = useExecution(executionId)

  if (detail.isPending) return <Skeleton className="h-32 w-full" />
  if (detail.isError)
    return <p className="text-destructive text-sm">Could not load this run.</p>

  const execution = detail.data
  if (!execution) return null

  return (
    <div className="space-y-3">
      {execution.error_message ? (
        <p className="text-destructive text-sm">{execution.error_message}</p>
      ) : null}

      {execution.hosts.length === 0 ? (
        <p className="text-muted-foreground text-sm">This run has no host rows.</p>
      ) : (
        execution.hosts.map((host) => (
          <div key={host.id} className="rounded-lg border p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-sm">{host.hostname}</span>
                <span className="text-muted-foreground text-xs">:{host.port}</span>
                <StatusBadge status={host.status} />
                {host.credential_name ? (
                  <Badge variant="outline">{host.credential_name}</Badge>
                ) : null}
              </div>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => onOpenTranscript(host)}
              >
                Log
              </Button>
            </div>

            {host.error_message ? (
              <p className="text-destructive mt-2 text-xs">{host.error_message}</p>
            ) : null}

            {host.steps.length > 0 ? (
              <ul className="mt-2 space-y-1">
                {host.steps.map((step) => (
                  <li
                    key={step.position}
                    className="flex flex-wrap items-center gap-2 text-xs"
                  >
                    <span className="text-muted-foreground w-5 text-right">
                      {step.position + 1}.
                    </span>
                    <span className="font-medium">{step.template_name}</span>
                    <StatusBadge status={step.status} />
                    {step.exit_code !== null ? (
                      <span className="text-muted-foreground">
                        exit {step.exit_code}
                      </span>
                    ) : null}
                    {step.duration_ms !== null ? (
                      <span className="text-muted-foreground">
                        {step.duration_ms} ms
                      </span>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ))
      )}
    </div>
  )
}

export function ExecutionsPage() {
  const [filter, setFilter] = useState<ExecutionStatus | 'all'>('all')
  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [transcript, setTranscript] = useState<{
    executionId: number
    host: HostRun
  } | null>(null)

  const list = useExecutions(filter)
  const items = list.data?.items ?? []

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Executions</h1>
          <p className="text-muted-foreground text-sm">
            Persistent run history with per-host and per-step results. Logs are
            stored on disk; this page keeps only the metadata.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => void list.refetch()}
          disabled={list.isFetching}
        >
          <RefreshCw className={list.isFetching ? 'size-4 animate-spin' : 'size-4'} />
          Refresh
        </Button>
      </div>

      <div className="flex flex-wrap gap-2">
        {FILTERS.map((option) => (
          <Button
            key={option}
            size="sm"
            variant={filter === option ? 'default' : 'outline'}
            onClick={() => setFilter(option)}
          >
            {option}
          </Button>
        ))}
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ListChecks className="size-4" />
            History
          </CardTitle>
          <CardDescription>
            {list.isPending
              ? 'Loading…'
              : `${list.data?.total ?? 0} run${list.data?.total === 1 ? '' : 's'}`}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {list.isPending ? (
            <>
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-16 w-full" />
            </>
          ) : list.isError ? (
            <p className="text-destructive text-sm">Could not load history.</p>
          ) : items.length === 0 ? (
            <p className="text-muted-foreground text-sm">
              No runs yet. Start one from a workflow.
            </p>
          ) : (
            items.map((execution) => (
              <div key={execution.id} className="rounded-lg border">
                <button
                  type="button"
                  onClick={() =>
                    setExpandedId(expandedId === execution.id ? null : execution.id)
                  }
                  className="hover:bg-muted/40 flex w-full flex-wrap items-center justify-between gap-2 p-3 text-left"
                >
                  <span className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{execution.workflow_name}</span>
                    <StatusBadge status={execution.status} />
                    {execution.trigger === 'scheduled' ? (
                      <Badge variant="secondary" className="gap-1">
                        <CalendarClock aria-hidden="true" />
                        {execution.schedule_name ?? 'scheduled'}
                      </Badge>
                    ) : (
                      <Badge variant="outline">manual</Badge>
                    )}
                  </span>
                  <span className="text-muted-foreground flex flex-wrap items-center gap-3 text-xs">
                    <span>
                      {execution.succeeded_host_count}/{execution.host_count} hosts ok
                    </span>
                    {execution.failed_host_count > 0 ? (
                      <span className="text-destructive">
                        {execution.failed_host_count} failed
                      </span>
                    ) : null}
                    <span>{formatDate(execution.started_at ?? execution.created_at)}</span>
                    <span>{formatDuration(execution.started_at, execution.finished_at)}</span>
                  </span>
                </button>

                {expandedId === execution.id ? (
                  <div className="border-t p-3">
                    <HostBreakdown
                      executionId={execution.id}
                      onOpenTranscript={(host) =>
                        setTranscript({ executionId: execution.id, host })
                      }
                    />
                  </div>
                ) : null}
              </div>
            ))
          )}
        </CardContent>
      </Card>

      <TranscriptDialog
        executionId={transcript?.executionId ?? 0}
        host={transcript?.host ?? null}
        onClose={() => setTranscript(null)}
      />
    </div>
  )
}
