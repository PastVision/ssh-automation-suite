/**
 * Host run transcript viewer.
 *
 * The bytes live on the filesystem; this renders the tail the API returns. It is
 * deliberately not a live stream: there is no WebSocket endpoint yet, so the
 * dialog refetches on open and shows a note when the transcript was truncated.
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
import type { HostRun } from '@/lib/types'

export function TranscriptDialog({
  executionId,
  host,
  onClose,
}: {
  executionId: number
  host: HostRun | null
  onClose: () => void
}) {
  const transcript = useHostTranscript(executionId, host?.id ?? null)

  return (
    <Dialog open={host !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-center gap-2">
            <FileText className="size-4" />
            {host?.hostname ?? 'Host'}
            {host ? <StatusBadge status={host.status} /> : null}
          </DialogTitle>
          <DialogDescription>
            Transcript for this host run, read from the execution log directory.
          </DialogDescription>
        </DialogHeader>

        {transcript.isPending ? (
          <Skeleton className="h-64 w-full" />
        ) : transcript.isError ? (
          <p className="text-sm text-destructive">Could not load this transcript.</p>
        ) : !transcript.data?.available ? (
          <p className="text-muted-foreground text-sm">
            No transcript yet. The host has not started, or its log was pruned.
          </p>
        ) : (
          <div className="space-y-2">
            {transcript.data.truncated ? (
              <p className="text-warning text-xs">
                Showing the end of a large transcript; earlier output was omitted.
              </p>
            ) : null}
            <pre className="bg-muted/50 max-h-[60vh] overflow-auto rounded-md border p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap">
              {transcript.data.content || '(empty)'}
            </pre>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
