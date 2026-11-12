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

import { Ban, CalendarClock, ListChecks, RefreshCw } from 'lucide-react'
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
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { Skeleton } from '@/components/ui/skeleton'
import {
  describeCancelError,
  isCancellable,
  useCancelExecution,
  useExecution,
  useExecutions,
} from '@/hooks/use-executions'
import { formatApiInstant, parseApiInstant } from '@/lib/display'
import { diagnoseSudoFailure } from '@/lib/sudo-diagnostics'
import type { ExecutionStatus, HostRun, LogVisibility } from '@/lib/types'

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
  onRequestCancel,
}: {
  executionId: number
  onOpenTranscript: (host: HostRun) => void
  onRequestCancel: () => void
}) {
  const detail = useExecution(executionId)

  if (detail.isPending) return <Skeleton className="h-32 w-full" />
  if (detail.isError)
    return <p className="text-destructive text-sm">Could not load this run.</p>

  const execution = detail.data
  if (!execution) return null

  const cancelling =
    execution.cancel_requested_at !== null && isCancellable(execution.status)
  const cancellable = isCancellable(execution.status) && !cancelling

  return (
    <div className="space-y-3">
      {execution.error_message ? (
        <p className="text-destructive text-sm">{execution.error_message}</p>
      ) : null}

      {/*
        The request is cooperative: the run keeps a non-terminal status until the
        worker actually stops, so "requested" is shown separately from "cancelled"
        rather than pretending the run is already over.
      */}
      {cancelling ? (
        <p className="text-warning text-sm" role="status">
          Cancellation requested
          {execution.cancel_requested_by_id !== null ? ' by an operator' : ''} at{' '}
          {formatDate(execution.cancel_requested_at)}. The run stops at the next
          safe point.
        </p>
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
              <HostFailure hostname={host.hostname} message={host.error_message} />
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

      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-muted-foreground text-xs">
          {cancellable
            ? 'Cancelling asks the run to stop; in-flight remote commands finish first.'
            : `Transcripts are ${describeLogVisibility(execution.log_visibility)}.`}
        </p>
        <Button
          variant="outline"
          size="sm"
          className="text-destructive hover:text-destructive"
          onClick={onRequestCancel}
          disabled={!cancellable}
          title={
            cancellable
              ? 'Request cancellation'
              : cancelling
                ? 'Cancellation already requested'
                : 'This run has already finished'
          }
        >
          <Ban /> {cancelling ? 'Cancelling…' : 'Cancel run'}
        </Button>
      </div>
    </div>
  )
}

/**
 * Who can read this run's transcripts.
 *
 * Shown because it is a real access decision the operator cannot see anywhere
 * else. There is no control for it: no route accepts a log-visibility change, so
 * offering a dropdown here would be a button that cannot work.
 */
function describeLogVisibility(visibility: LogVisibility): string {
  switch (visibility) {
    case 'public':
      return 'readable by every signed-in user'
    case 'shared':
      return 'readable by users this workflow is shared with'
    default:
      return 'readable only by you'
  }
}

/**
 * A host's failure, with the actionable part pulled out when the backend has
 * already classified a privilege problem.
 *
 * The raw message is always shown: it is what the transcript footer says, and an
 * operator comparing the two must not find different text. The diagnosis adds
 * *what to change* next to it rather than replacing it.
 */
function HostFailure({
  hostname,
  message,
}: {
  hostname: string
  message: string
}) {
  const diagnosis = diagnoseSudoFailure(message, hostname)
  if (!diagnosis) {
    return <p className="text-destructive mt-2 text-xs">{message}</p>
  }
  return (
    <div className="border-destructive/40 bg-destructive/5 mt-2 rounded-md border p-2">
      <p className="text-destructive text-xs font-medium">{diagnosis.summary}</p>
      <p className="text-muted-foreground mt-1 text-xs">{diagnosis.action}</p>
      <details className="mt-1">
        <summary className="text-muted-foreground cursor-pointer text-xs underline underline-offset-2">
          Backend message
        </summary>
        <p className="text-muted-foreground mt-1 text-xs whitespace-pre-wrap">
          {message}
        </p>
      </details>
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
  const [confirmCancel, setConfirmCancel] = useState<{
    executionId: number
    workflowName: string
    hostCount: number
  } | null>(null)
  const [cancelError, setCancelError] = useState<string | null>(null)

  const list = useExecutions(filter)
  const items = list.data?.items ?? []
  const cancel = useCancelExecution()

  function openCancel(execution: {
    id: number
    workflow_name: string
    host_count: number
  }) {
    setCancelError(null)
    setConfirmCancel({
      executionId: execution.id,
      workflowName: execution.workflow_name,
      hostCount: execution.host_count,
    })
  }

  async function submitCancel() {
    if (!confirmCancel) return
    setCancelError(null)
    try {
      await cancel.mutateAsync(confirmCancel.executionId)
      setConfirmCancel(null)
    } catch (error) {
      // Kept open with the reason so the operator can decide what to do next
      // instead of losing the dialog and guessing why nothing happened.
      setCancelError(describeCancelError(error))
    }
  }

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
                      onRequestCancel={() => openCancel(execution)}
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

      <ConfirmDialog
        open={confirmCancel !== null}
        title="Cancel this run?"
        description={
          confirmCancel
            ? `Run #${confirmCancel.executionId} ("${confirmCancel.workflowName}") is targeting ${confirmCancel.hostCount} host${
                confirmCancel.hostCount === 1 ? '' : 's'
              }. Cancellation is cooperative: a remote command already running finishes before that host stops, and hosts not yet started are skipped.`
            : ''
        }
        confirmLabel="Cancel run"
        cancelLabel="Keep running"
        pending={cancel.isPending}
        destructive
        error={cancelError}
        onConfirm={() => void submitCancel()}
        onOpenChange={(open) => {
          if (!open && !cancel.isPending) {
            setConfirmCancel(null)
            setCancelError(null)
          }
        }}
      />
    </div>
  )
}
