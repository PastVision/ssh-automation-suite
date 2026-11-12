/**
 * The next few firings of a timing definition, as computed by the backend.
 *
 * This renders `/api/schedules/preview` and nothing else. It must not derive
 * dates locally: the whole reason the endpoint exists is that a cron expression
 * is unreadable, and a second implementation would drift from the dispatcher.
 */

import { Skeleton } from '@/components/ui/skeleton'
import { useSchedulePreview } from '@/hooks/use-schedules'
import { ApiError } from '@/lib/api-client'
import { formatApiInstant } from '@/lib/display'
import type { ScheduleKind } from '@/lib/types'

export function SchedulePreview({
  scheduleKind,
  timezone,
  cronExpression,
  runAt,
}: {
  scheduleKind: ScheduleKind
  timezone: string
  cronExpression: string | null
  runAt: string | null
}) {
  const preview = useSchedulePreview({
    schedule_kind: scheduleKind,
    timezone,
    cron_expression: cronExpression,
    run_at: runAt,
    enabled: Boolean(timezone) && (runAt !== null || Boolean(cronExpression)),
  })

  if (preview.isPending) return <Skeleton className="h-12 w-full" />

  if (preview.isError) {
    const detail =
      preview.error instanceof ApiError
        ? (preview.error.detail ?? 'This timing definition is not valid.')
        : 'This timing definition is not valid.'
    return <p className="text-destructive text-xs">{detail}</p>
  }

  const items = preview.data?.items ?? []
  if (items.length === 0) {
    return (
      <p className="text-muted-foreground text-xs">
        This definition never fires. Check the date or expression.
      </p>
    )
  }

  return (
    <ul className="space-y-1 text-xs">
      {items.map((firing) => (
        <li key={firing.scheduled_for} className="flex flex-wrap gap-x-2">
          <span className="font-medium">
            {formatApiInstant(firing.scheduled_for, timezone)}
          </span>
          <span className="text-muted-foreground">{timezone}</span>
        </li>
      ))}
    </ul>
  )
}
