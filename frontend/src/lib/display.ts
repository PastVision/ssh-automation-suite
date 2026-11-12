/**
 * Display helpers shared by the shell and the pages.
 *
 * The `parseApiInstant` / `formatApiInstant` pair exists because the API sends
 * offset-less UTC timestamps; see the note on `parseApiInstant` before using
 * `new Date()` on a value that came from the backend.
 */

import type { User } from '@/lib/types'

/** Best-effort display name: accounts may have no display name set. */
export function displayNameOf(user: User | null | undefined): string {
  if (!user) return 'Unknown user'
  return user.display_name?.trim() || user.username
}

/** Two-letter monogram for the avatar; never more than two characters. */
export function initialsOf(user: User | null | undefined): string {
  if (!user) return '?'
  const parts = displayNameOf(user)
    .split(/[\s._-]+/)
    .filter(Boolean)
  if (parts.length === 0) return '?'
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[1][0]).toUpperCase()
}

/**
 * Parse a timestamp from the API.
 *
 * SQLite stores these without an offset (`2026-11-13T03:00:00`), and the value
 * is always UTC. `new Date()` would read such a string as *local* time, which
 * shifts the clock by the browser's offset -- a 03:00 UTC schedule rendered as
 * 21:30 for a viewer in UTC-5:30. Appending `Z` when no offset is present keeps
 * the instant correct; strings that already carry one are left alone.
 */
export function parseApiInstant(value: string | null | undefined): Date | null {
  if (!value) return null
  const hasOffset = /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(value)
  const parsed = new Date(hasOffset ? value : `${value}Z`)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

/**
 * Render an API instant, optionally in a specific IANA zone. Without a zone the
 * viewer's own zone is used, which is right for records like "created at".
 * An unloadable zone falls back to the viewer's zone rather than throwing.
 */
export function formatApiInstant(
  value: string | null | undefined,
  timezone?: string,
): string {
  const parsed = parseApiInstant(value)
  if (!parsed) return '—'
  if (timezone) {
    try {
      return parsed.toLocaleString(undefined, {
        dateStyle: 'medium',
        timeStyle: 'short',
        timeZone: timezone,
      })
    } catch {
      // Fall through to the viewer's zone.
    }
  }
  return parsed.toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

/** The wall-clock offset of `timezone` at a given instant, in milliseconds. */
function zoneOffsetMs(instantMs: number, timezone: string): number {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: timezone,
    hourCycle: 'h23',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).formatToParts(new Date(instantMs))

  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value)
  const asUtc = Date.UTC(
    get('year'),
    get('month') - 1,
    get('day'),
    get('hour'),
    get('minute'),
    get('second'),
  )
  return asUtc - instantMs
}

/**
 * Read a `datetime-local` value (`YYYY-MM-DDTHH:mm`) as wall-clock time in
 * `timezone`, returning the equivalent UTC instant as an ISO string.
 *
 * This matters because the API treats an offset-less timestamp as UTC. Sending
 * the raw input would fire a "09:00" one-time schedule at 09:00 UTC regardless
 * of the zone the form says it is read in. Two passes settle the offset so a
 * value on a DST boundary lands on the right side of the change.
 */
export function zonedInputToIso(
  local: string,
  timezone: string,
): string | null {
  if (!local) return null
  // `datetime-local` omits seconds; treat the text as if it were UTC to get a
  // stable starting point, then correct by the zone's offset.
  const guess = Date.parse(`${local}:00Z`)
  if (Number.isNaN(guess)) return null

  let offset: number
  try {
    offset = zoneOffsetMs(guess, timezone)
    offset = zoneOffsetMs(guess - offset, timezone)
  } catch {
    return null
  }
  return new Date(guess - offset).toISOString()
}

/** The inverse: an API instant as `datetime-local` text in `timezone`. */
export function isoToZonedInput(
  value: string | null | undefined,
  timezone: string,
): string {
  const parsed = parseApiInstant(value)
  if (!parsed) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: timezone,
      hourCycle: 'h23',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    }).formatToParts(parsed)
    const get = (type: string) =>
      parts.find((p) => p.type === type)?.value ?? '00'
    return `${get('year')}-${get('month')}-${get('day')}T${pad(Number(get('hour')))}:${pad(Number(get('minute')))}`
  } catch {
    return ''
  }
}
