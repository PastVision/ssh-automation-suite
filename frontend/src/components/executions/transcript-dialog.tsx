/**
 * Host run transcript viewer.
 *
 * A host that is still writing is followed over a WebSocket so output appears as
 * it happens; a host that has finished keeps the HTTP snapshot, because the whole
 * file is already on disk and a stream would add nothing. The snapshot is also
 * the fallback whenever the live connection cannot be established, so the dialog
 * always shows something.
 */

import { FileText } from 'lucide-react'

import { StatusBadge } from '@/components/executions/status-badge'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { useHostTranscript } from '@/hooks/use-executions'
import { useTranscriptStream } from '@/hooks/use-transcript-stream'
import type { HostRun } from '@/lib/types'

const LIVE_HOST_STATUSES = new Set<HostRun['status']>(['pending', 'running'])

export function TranscriptDialog({
  executionId,
  host,
  onClose,
}: {
  executionId: number
  host: HostRun | null
  onClose: () => void
}) {
  const hostRunId = host?.id ?? null
  const live = host !== null && LIVE_HOST_STATUSES.has(host.status)

  const stream = useTranscriptStream(executionId, hostRunId, { enabled: live })

  // Also fetched for a live host: it is the fallback while the socket is still
  // connecting, and the only source if the socket is refused outright.
  const snapshot = useHostTranscript(executionId, hostRunId)

  const streamFailed = stream.status === 'error'
  const showSnapshot = !live || streamFailed
  const content = showSnapshot ? snapshot.data?.content : stream.content

  const unavailable =
    showSnapshot && !snapshot.isPending && !snapshot.isError && !snapshot.data?.available

  return (
    <Dialog open={host !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-center gap-2">
            <FileText className="size-4" />
            {host?.hostname ?? 'Host'}
            {host ? <StatusBadge status={host.status} /> : null}
            {live && stream.status === 'live' ? (
              <span className="text-success text-xs font-normal">● live</span>
            ) : null}
            {live && stream.status === 'connecting' ? (
              <span className="text-muted-foreground text-xs font-normal">connecting…</span>
            ) : null}
          </DialogTitle>
          <DialogDescription>
            {live
              ? 'Following this host run as it writes output.'
              : 'Transcript for this host run, read from the execution log directory.'}
          </DialogDescription>
        </DialogHeader>

        {showSnapshot && snapshot.isPending ? (
          <Skeleton className="h-64 w-full" />
        ) : showSnapshot && snapshot.isError ? (
          <p className="text-sm text-destructive">Could not load this transcript.</p>
        ) : unavailable ? (
          <p className="text-muted-foreground text-sm">
            No transcript yet. The host has not started, or its log was pruned.
          </p>
        ) : (
          <div className="space-y-2">
            <TranscriptNotices
              truncated={showSnapshot ? Boolean(snapshot.data?.truncated) : stream.truncated}
              missingBytes={stream.missingBytes}
              droppedBytes={stream.droppedBytes}
              error={streamFailed ? stream.error : null}
            />
            <pre className="bg-muted/50 max-h-[60vh] overflow-auto rounded-md border p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap">
              {content || '(empty)'}
            </pre>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

function TranscriptNotices({
  truncated,
  missingBytes,
  droppedBytes,
  error,
}: {
  truncated: boolean
  missingBytes: number
  droppedBytes: number
  error: string | null
}) {
  return (
    <>
      {error ? (
        <p className="text-destructive text-xs">
          {error} Showing the last snapshot instead.
        </p>
      ) : null}
      {truncated ? (
        <p className="text-warning text-xs">
          Showing the end of a large transcript; earlier output was omitted.
        </p>
      ) : null}
      {missingBytes > 0 ? (
        <p className="text-warning text-xs">
          {missingBytes} bytes were missed before the connection caught up.
        </p>
      ) : null}
      {droppedBytes > 0 ? (
        <p className="text-warning text-xs">
          {droppedBytes} bytes were dropped because this view fell behind.
        </p>
      ) : null}
    </>
  )
}
