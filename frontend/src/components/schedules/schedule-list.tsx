/**
 * Schedules belonging to one workflow.
 *
 * Every row shows the server's `next_run_at` rather than a locally derived
 * time, and a paused schedule shows why it was paused: a schedule the scheduler
 * parked because its owner went inactive looks identical to one the user paused
 * unless the reason is surfaced.
 */

import { CalendarClock, Pause, Pencil, Play, Trash2 } from 'lucide-react'
import { useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { useDeleteSchedule, useSchedules, useSetScheduleEnabled } from '@/hooks/use-schedules'
import { ApiError } from '@/lib/api-client'
import { formatApiInstant } from '@/lib/display'
import type { Schedule, ScheduleState } from '@/lib/types'

const STATE_VARIANT: Record<
  ScheduleState,
  'default' | 'secondary' | 'destructive' | 'outline' | 'success' | 'warning'
> = {
  scheduled: 'outline',
  active: 'success',
  paused: 'warning',
  disabled: 'secondary',
  completed: 'secondary',
  failed: 'destructive',
  missed: 'warning',
}

const PAUSED_REASON: Record<string, string> = {
  owner_inactive: 'owner inactive',
  invalid_workflow: 'workflow no longer valid',
  unauthorized: 'no longer authorized',
}

/** A schedule's timing in the form a user recognises, without re-deriving it. */
function describeSchedule(schedule: Schedule): string {
  if (schedule.schedule_kind === 'once') {
    return schedule.run_at
      ? `Once at ${formatApiInstant(schedule.run_at, schedule.timezone)}`
      : 'Once'
  }
  if (schedule.schedule_kind === 'cron') {
    return schedule.cron_expression
      ? `Cron ${schedule.cron_expression}`
      : 'Cron'
  }
  const label =
    schedule.schedule_kind === 'daily'
      ? 'Daily'
      : schedule.schedule_kind === 'weekly'
        ? 'Weekly'
        : 'Monthly'
  return schedule.cron_expression
    ? `${label} — ${schedule.cron_expression}`
    : label
}

export function ScheduleList({
  workflowId,
  onEdit,
}: {
  workflowId: number
  onEdit: (schedule: Schedule) => void
}) {
  const list = useSchedules(workflowId)
  const toggle = useSetScheduleEnabled()
  const remove = useDeleteSchedule()
  const [error, setError] = useState<string | null>(null)

  const items = list.data?.items ?? []

  async function handleToggle(schedule: Schedule) {
    setError(null)
    try {
      await toggle.mutateAsync({
        id: schedule.id,
        enabled: !schedule.enabled,
        row_version: schedule.row_version,
      })
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? (caught.detail ?? 'Could not change this schedule.')
          : 'Could not change this schedule.',
      )
    }
  }

  async function handleDelete(schedule: Schedule) {
    const confirmed = window.confirm(
      `Delete the schedule "${schedule.name}"? Runs it already produced keep their history.`,
    )
    if (!confirmed) return
    setError(null)
    try {
      await remove.mutateAsync(schedule.id)
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? (caught.detail ?? 'Could not delete this schedule.')
          : 'Could not delete this schedule.',
      )
    }
  }

  if (list.isPending) return <Skeleton className="h-16 w-full" />
  if (list.isError) {
    return <p className="text-destructive text-sm">Could not load schedules.</p>
  }

  if (items.length === 0) {
    return (
      <div className="flex flex-col items-start gap-1 rounded-lg border border-dashed p-4">
        <span className="flex items-center gap-2 text-sm font-medium">
          <CalendarClock className="size-4" aria-hidden="true" />
          No schedules
        </span>
        <p className="text-muted-foreground text-xs">
          This workflow only runs when you start it by hand.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-2">
      {error ? <p className="text-destructive text-sm">{error}</p> : null}
      {items.map((schedule) => (
        <div key={schedule.id} className="rounded-lg border p-3">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0 space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium">{schedule.name}</span>
                <Badge variant={STATE_VARIANT[schedule.state]}>
                  {schedule.state}
                </Badge>
                {schedule.paused_reason ? (
                  <Badge variant="outline">
                    {PAUSED_REASON[schedule.paused_reason] ??
                      schedule.paused_reason}
                  </Badge>
                ) : null}
              </div>
              <p className="text-muted-foreground text-xs">
                {describeSchedule(schedule)} · {schedule.timezone}
              </p>
              <p className="text-xs">
                <span className="text-muted-foreground">Next run: </span>
                <span className="font-medium">
                  {schedule.enabled
                    ? formatApiInstant(schedule.next_run_at, schedule.timezone)
                    : 'paused'}
                </span>
              </p>
            </div>

            <div className="flex items-center gap-1">
              <Button
                variant="outline"
                size="sm"
                onClick={() => void handleToggle(schedule)}
                disabled={toggle.isPending}
              >
                {schedule.enabled ? <Pause /> : <Play />}
                {schedule.enabled ? 'Pause' : 'Resume'}
              </Button>
              <Button variant="ghost" size="sm" onClick={() => onEdit(schedule)}>
                <Pencil />
                Edit
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="text-destructive hover:text-destructive"
                onClick={() => void handleDelete(schedule)}
                disabled={remove.isPending}
                title="Delete schedule"
              >
                <Trash2 />
              </Button>
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}
