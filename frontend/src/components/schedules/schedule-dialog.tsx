/**
 * Create/edit dialog for one schedule.
 *
 * The form owns no timing semantics. `daily`, `weekly` and `monthly` exist only
 * to spare the user from writing cron by hand: each one is translated into the
 * plain five-field expression the backend stores, and everything that decides
 * *when* the thing actually fires -- validation, timezone resolution, DST, the
 * next firing -- stays on the server. The preview below the form is the
 * server's answer, not a second calculation.
 */

import { useState, type FormEvent } from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { useCreateSchedule, useUpdateSchedule } from '@/hooks/use-schedules'
import { ApiError } from '@/lib/api-client'
import { isoToZonedInput, zonedInputToIso } from '@/lib/display'
import type { MisfirePolicy, Schedule, ScheduleInput, ScheduleKind } from '@/lib/types'

import { SchedulePreview } from './schedule-preview'

const KINDS: Array<{ value: ScheduleKind; label: string }> = [
  { value: 'once', label: 'Once' },
  { value: 'daily', label: 'Daily' },
  { value: 'weekly', label: 'Weekly' },
  { value: 'monthly', label: 'Monthly' },
  { value: 'cron', label: 'Cron' },
]

const MISFIRE: Array<{ value: MisfirePolicy; label: string; hint: string }> = [
  {
    value: 'skip',
    label: 'Skip it',
    hint: 'A firing missed while the app was down is recorded and not run.',
  },
  {
    value: 'run_once',
    label: 'Run once on recovery',
    hint: 'Catch up a single missed firing when the app comes back.',
  },
]

/**
 * A short list of common zones, not the full database. The field stays free
 * text and the backend rejects a zone it cannot load with a message this form
 * shows verbatim.
 */
const COMMON_TIMEZONES = [
  'UTC',
  'Europe/London',
  'Europe/Berlin',
  'Europe/Paris',
  'Europe/Madrid',
  'Europe/Warsaw',
  'Europe/Moscow',
  'America/New_York',
  'America/Chicago',
  'America/Denver',
  'America/Los_Angeles',
  'America/Sao_Paulo',
  'Asia/Dubai',
  'Asia/Kolkata',
  'Asia/Singapore',
  'Asia/Shanghai',
  'Asia/Tokyo',
  'Australia/Sydney',
]

const WEEKDAYS = [
  { value: '0', label: 'Sunday' },
  { value: '1', label: 'Monday' },
  { value: '2', label: 'Tuesday' },
  { value: '3', label: 'Wednesday' },
  { value: '4', label: 'Thursday' },
  { value: '5', label: 'Friday' },
  { value: '6', label: 'Saturday' },
]

/** Mirrors the backend schema default so the form and the API agree. */
const DEFAULT_GRACE = 300

const FIELD = 'border-input bg-background w-full rounded-md border px-3 py-2 text-sm'

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

/** Build the expression for the simple kinds, or null when incomplete. */
function toCron(
  kind: ScheduleKind,
  time: string,
  weekday: string,
  dayOfMonth: string,
): string | null {
  if (kind === 'cron' || kind === 'once') return null
  const parts = time.split(':')
  if (parts.length !== 2) return null
  const hours = Number(parts[0])
  const minutes = Number(parts[1])
  if (!Number.isInteger(hours) || !Number.isInteger(minutes)) return null
  if (hours > 23 || minutes > 59) return null
  const head = `${minutes} ${hours}`
  if (kind === 'daily') return `${head} * * *`
  if (kind === 'weekly') return `${head} * * ${weekday}`
  const day = Number(dayOfMonth)
  if (!Number.isInteger(day) || day < 1 || day > 31) return null
  return `${head} ${day} * *`
}

interface SimpleForm {
  kind: ScheduleKind
  time: string
  weekday: string
  dayOfMonth: string
}

/**
 * Recover the simple controls from a stored expression, so editing a daily
 * schedule opens on the daily controls. An expression that does not match one
 * of those shapes returns null and the form falls back to raw cron rather than
 * silently discarding part of what the user wrote.
 */
function fromCron(expression: string | null): SimpleForm | null {
  if (!expression) return null
  const fields = expression.trim().split(/\s+/)
  if (fields.length !== 5) return null
  const [minute, hour, dom, month, dow] = fields
  const numeric = (value: string) => /^\d{1,2}$/.test(value)
  if (!numeric(minute) || !numeric(hour) || month !== '*') return null
  const time = `${pad(Number(hour))}:${pad(Number(minute))}`
  if (dom === '*' && dow === '*') {
    return { kind: 'daily', time, weekday: '1', dayOfMonth: '1' }
  }
  if (dom === '*' && numeric(dow)) {
    return { kind: 'weekly', time, weekday: dow, dayOfMonth: '1' }
  }
  if (dow === '*' && numeric(dom)) {
    return { kind: 'monthly', time, weekday: '1', dayOfMonth: dom }
  }
  return null
}

/** `datetime-local` needs `YYYY-MM-DDTHH:mm`, in the schedule's zone. */
function toZonedInput(value: string | null, timezone: string): string {
  return isoToZonedInput(value, timezone)
}

export function ScheduleDialog({
  open,
  onOpenChange,
  workflowId,
  workflowName,
  schedule,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  workflowId: number
  workflowName: string
  /** Present when editing, absent when creating. */
  schedule?: Schedule | null
}) {
  const editing = schedule ?? null
  const restore = fromCron(editing?.cron_expression ?? null)
  const asRawCron = Boolean(editing?.cron_expression) && restore === null

  const [name, setName] = useState(editing?.name ?? `${workflowName} schedule`)
  const [kind, setKind] = useState<ScheduleKind>(
    editing?.schedule_kind ?? 'daily',
  )
  const [time, setTime] = useState(restore?.time ?? '03:00')
  const [weekday, setWeekday] = useState(restore?.weekday ?? '1')
  const [dayOfMonth, setDayOfMonth] = useState(restore?.dayOfMonth ?? '1')
  const [cron, setCron] = useState(
    asRawCron ? (editing?.cron_expression ?? '') : '0 3 * * *',
  )
  const [timezone, setTimezone] = useState(editing?.timezone ?? 'UTC')
  const [runAt, setRunAt] = useState(toZonedInput(editing?.run_at ?? null, editing?.timezone ?? 'UTC'))
  const [policy, setPolicy] = useState<MisfirePolicy>(
    editing?.misfire_policy ?? 'skip',
  )
  const [grace, setGrace] = useState(
    String(editing?.misfire_grace_seconds ?? DEFAULT_GRACE),
  )
  const [error, setError] = useState<string | null>(null)

  const create = useCreateSchedule()
  const update = useUpdateSchedule(editing?.id ?? 0)
  const saving = create.isPending || update.isPending

  const expression =
    kind === 'cron' ? cron.trim() : toCron(kind, time, weekday, dayOfMonth)

  // The input holds wall clock in `timezone`; the API takes an instant. Convert
  // once here so the preview and the save send the same value.
  const runAtIso =
    kind === 'once' ? zonedInputToIso(runAt, timezone.trim() || 'UTC') : null

  const previewable =
    timezone.trim() !== '' && (kind === 'once' ? runAtIso !== null : Boolean(expression))

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)

    const payload: ScheduleInput = {
      name: name.trim(),
      workflow_id: workflowId,
      schedule_kind: kind,
      timezone: timezone.trim(),
      // The API rejects a one-time schedule carrying an expression and a
      // recurring one carrying a run time, so exactly one is ever sent.
      cron_expression: kind === 'once' ? null : expression,
      run_at: kind === 'once' ? runAtIso : null,
      misfire_policy: policy,
      misfire_grace_seconds: Number(grace),
    }

    try {
      if (editing) {
        await update.mutateAsync({ ...payload, row_version: editing.row_version })
      } else {
        await create.mutateAsync(payload)
      }
      onOpenChange(false)
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? (caught.detail ?? 'Could not save this schedule.')
          : 'Could not save this schedule.',
      )
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto">
        <form onSubmit={handleSubmit} className="space-y-4">
          <DialogHeader>
            <DialogTitle>{editing ? 'Edit schedule' : 'New schedule'}</DialogTitle>
            <DialogDescription>
              Runs <span className="font-medium">{workflowName}</span> on a
              timetable. Times are read in the timezone you choose.
            </DialogDescription>
          </DialogHeader>

          <label className="block space-y-1 text-sm">
            <span className="font-medium">Name</span>
            <input
              required
              maxLength={120}
              value={name}
              onChange={(event) => setName(event.target.value)}
              className={FIELD}
            />
          </label>

          <div className="space-y-2">
            <span className="text-sm font-medium">Repeats</span>
            <div className="flex flex-wrap gap-2">
              {KINDS.map((option) => (
                <Button
                  key={option.value}
                  type="button"
                  size="sm"
                  variant={kind === option.value ? 'default' : 'outline'}
                  onClick={() => setKind(option.value)}
                >
                  {option.label}
                </Button>
              ))}
            </div>
          </div>

          {kind === 'once' ? (
            <label className="block space-y-1 text-sm">
              <span className="font-medium">Run at</span>
              <input
                required
                type="datetime-local"
                value={runAt}
                onChange={(event) => setRunAt(event.target.value)}
                className={FIELD}
              />
              <span className="text-muted-foreground block text-xs">
                A one-time schedule completes once it has fired.
              </span>
            </label>
          ) : kind === 'cron' ? (
            <label className="block space-y-1 text-sm">
              <span className="font-medium">Cron expression</span>
              <input
                required
                value={cron}
                onChange={(event) => setCron(event.target.value)}
                placeholder="0 3 * * *"
                spellCheck={false}
                className={`${FIELD} font-mono`}
              />
              <span className="text-muted-foreground block text-xs">
                Five fields: minute hour day-of-month month day-of-week.
              </span>
            </label>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block space-y-1 text-sm">
                <span className="font-medium">Time</span>
                <input
                  required
                  type="time"
                  value={time}
                  onChange={(event) => setTime(event.target.value)}
                  className={FIELD}
                />
              </label>
              {kind === 'weekly' ? (
                <label className="block space-y-1 text-sm">
                  <span className="font-medium">Day</span>
                  <select
                    value={weekday}
                    onChange={(event) => setWeekday(event.target.value)}
                    className={FIELD}
                  >
                    {WEEKDAYS.map((day) => (
                      <option key={day.value} value={day.value}>
                        {day.label}
                      </option>
                    ))}
                  </select>
                </label>
              ) : null}
              {kind === 'monthly' ? (
                <label className="block space-y-1 text-sm">
                  <span className="font-medium">Day of month</span>
                  <input
                    required
                    type="number"
                    min={1}
                    max={31}
                    value={dayOfMonth}
                    onChange={(event) => setDayOfMonth(event.target.value)}
                    className={FIELD}
                  />
                </label>
              ) : null}
            </div>
          )}

          <label className="block space-y-1 text-sm">
            <span className="font-medium">Timezone</span>
            <input
              required
              list="schedule-timezones"
              value={timezone}
              onChange={(event) => setTimezone(event.target.value)}
              placeholder="Europe/Berlin"
              spellCheck={false}
              className={FIELD}
            />
            <datalist id="schedule-timezones">
              {COMMON_TIMEZONES.map((zone) => (
                <option key={zone} value={zone} />
              ))}
            </datalist>
            <span className="text-muted-foreground block text-xs">
              An IANA name. Daylight-saving changes follow this zone.
            </span>
          </label>

          <div className="space-y-2">
            <span className="text-sm font-medium">If a run is missed</span>
            <div className="space-y-2">
              {MISFIRE.map((option) => (
                <label key={option.value} className="flex items-start gap-2 text-sm">
                  <input
                    type="radio"
                    name="misfire"
                    value={option.value}
                    checked={policy === option.value}
                    onChange={() => setPolicy(option.value)}
                    className="mt-1"
                  />
                  <span>
                    <span className="font-medium">{option.label}</span>
                    <span className="text-muted-foreground block text-xs">
                      {option.hint}
                    </span>
                  </span>
                </label>
              ))}
            </div>
            <label className="block space-y-1 text-sm">
              <span className="font-medium">Grace period (seconds)</span>
              <input
                required
                type="number"
                min={0}
                max={86400}
                value={grace}
                onChange={(event) => setGrace(event.target.value)}
                className={FIELD}
              />
              <span className="text-muted-foreground block text-xs">
                How late a firing may start and still count as on time.
              </span>
            </label>
          </div>

          <div className="rounded-lg border p-3">
            <p className="mb-2 text-xs font-medium tracking-wide uppercase">
              Next runs
            </p>
            {previewable ? (
              <SchedulePreview
                scheduleKind={kind}
                timezone={timezone.trim()}
                cronExpression={kind === 'once' ? null : expression}
                runAt={kind === 'once' ? runAtIso : null}
              />
            ) : (
              <p className="text-muted-foreground text-xs">
                Fill in the timing above to see when this will run.
              </p>
            )}
          </div>

          {error ? <p className="text-destructive text-sm">{error}</p> : null}

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={saving}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={saving}>
              {saving ? 'Saving…' : editing ? 'Save changes' : 'Create schedule'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
