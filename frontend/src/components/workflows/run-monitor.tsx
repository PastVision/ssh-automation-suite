/**
 * Live view of a single execution, shown after a workflow launch.
 *
 * This is a light monitor, not a second history page: it polls the execution the
 * launch returned, shows per-host progress, and links out to the full Executions
 * page for transcripts. HTTP polling is used because the runner has no WebSocket
 * push yet; the interval stops as soon as the run reaches a terminal state.
 */

import { ExternalLink, RefreshCw } from 'lucide-react'

import { StatusBadge } from '@/components/executions/status-badge'
import { TranscriptDialog } from '@/components/executions/transcript-dialog'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { useExecution } from '@/hooks/use-executions'
import type { HostRun } from '@/lib/types'
import { useState } from 'react'

export function RunMonitor({
  executionId,
  onDismiss,
}: {
  executionId: number
  onDismiss: () => void
}) {
  const detail = useExecution(executionId)
  const [transcript, setTranscript] = useState<HostRun | null>(null)

  const execution = detail.data
  const active =
    execution?.status === 'pending' || execution?.status === 'running'

  return (
    <Card className="border-primary/30">
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <CardTitle className="flex items-center gap-2 text-base">
              Run #{executionId}
              {execution ? <StatusBadge status={execution.status} /> : null}
            </CardTitle>
            <CardDescription>
              {execution
                ? `${execution.succeeded_host_count}/${execution.host_count} hosts ok${
                    execution.failed_host_count > 0
                      ? ` · ${execution.failed_host_count} failed`
                      : ''
                  }`
                : 'Loading run state…'}
            </CardDescription>
          </div>
          <div className="flex items-center gap-1.5">
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void detail.refetch()}
              disabled={detail.isFetching}
            >
              <RefreshCw className={detail.isFetching ? 'size-4 animate-spin' : 'size-4'} />
              Refresh
            </Button>
            <Button variant="ghost" size="sm" onClick={onDismiss}>
              Close
            </Button>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-3">
        {detail.isPending ? (
          <Skeleton className="h-24 w-full" />
        ) : detail.isError ? (
          <p className="text-destructive text-sm">Could not load this run.</p>
        ) : execution ? (
          <>
            {execution.error_message ? (
              <p className="text-destructive text-sm">{execution.error_message}</p>
            ) : null}

            {execution.hosts.length === 0 ? (
              <p className="text-muted-foreground text-sm">
                {active ? 'Waiting for hosts to start…' : 'This run produced no host rows.'}
              </p>
            ) : (
              <div className="space-y-2">
                {execution.hosts.map((host) => (
                  <div key={host.id} className="rounded-lg border p-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-sm">{host.hostname}</span>
                        <span className="text-muted-foreground text-xs">:{host.port}</span>
                        <StatusBadge status={host.status} />
                      </span>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setTranscript(host)}
                      >
                        Log
                      </Button>
                    </div>

                    {host.error_message ? (
                      <p className="text-destructive mt-1 text-xs">{host.error_message}</p>
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
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </div>
                ))}
              </div>
            )}

            <p className="text-muted-foreground text-xs">
              Logs are written per host on the filesystem.{' '}
              <a
                href="/executions"
                className="inline-flex items-center gap-1 underline underline-offset-2"
              >
                Open Executions <ExternalLink className="size-3" />
              </a>
            </p>
          </>
        ) : null}
      </CardContent>

      <TranscriptDialog
        executionId={executionId}
        host={transcript}
        onClose={() => setTranscript(null)}
      />
    </Card>
  )
}
