import { AlertCircle, History, Monitor, ShieldCheck } from 'lucide-react'
import { useState } from 'react'

import { AccountCard } from '@/components/layout/user-menu'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { useAuditEvents } from '@/hooks/use-audit'
import { useEndSession, useSessions } from '@/hooks/use-sessions'
import { useAuth } from '@/lib/auth'

function formatWhen(iso: string | null): string {
  if (!iso) return '—'
  const parsed = new Date(iso)
  if (Number.isNaN(parsed.getTime())) return iso
  return parsed.toLocaleString()
}


/**
 * The audit trail is append-only server-side, so this is a faithful history
 * rather than a best-effort log. It is also self-scoped: the endpoint has no
 * parameter that could address another user.
 */
export function AccountPage() {
  const { user, session } = useAuth()
  const audit = useAuditEvents(100)
  const sessions = useSessions()
  const endSession = useEndSession()

  const [confirmId, setConfirmId] = useState<number | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const others = (sessions.data ?? []).filter((item) => item.revoked_at === null && item.id !== session?.id)

  async function handleEnd(sessionId: number) {
    setNotice(null)
    try {
      await endSession.mutateAsync(sessionId)
      setNotice('Session ended. That browser will need to sign in again.')
    } catch {
      setNotice('Could not end that session.')
    } finally {
      setConfirmId(null)
    }
  }

  return (
    <div className="space-y-6">
      <AccountCard />

      <section className="space-y-3 rounded-lg border p-4">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <ShieldCheck aria-hidden="true" className="size-4" />
            Current session
          </h2>
          <p className="text-muted-foreground text-xs">
            Sessions expire after inactivity and have a hard maximum lifetime, whichever
            comes first.
          </p>
        </div>
        {session ? (
          <dl className="grid gap-2 text-sm sm:grid-cols-2">
            <div>
              <dt className="text-muted-foreground text-xs">Signed in</dt>
              <dd>{formatWhen(session.created_at)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground text-xs">Expires no later than</dt>
              <dd>{formatWhen(session.expires_at)}</dd>
            </div>
            <div className="sm:col-span-2">
              <dt className="text-muted-foreground text-xs">Signed in from</dt>
              <dd className="font-mono text-xs">{session.remote_addr ?? '—'}</dd>
            </div>
          </dl>
        ) : (
          <p className="text-muted-foreground text-sm">
            This deployment authenticates through a proxy, so there is no local session
            record.
          </p>
        )}
      </section>

      <section className="space-y-3 rounded-lg border p-4">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <Monitor aria-hidden="true" className="size-4" />
            Other active sessions
          </h2>
          <p className="text-muted-foreground text-xs">
            One row per signed-in browser. Ending a session also releases the edit locks
            it was holding.
          </p>
        </div>

        {notice ? (
          <p role="status" className="text-muted-foreground text-sm">
            {notice}
          </p>
        ) : null}

        {sessions.isPending ? (
          <Skeleton className="h-16 w-full" />
        ) : others.length === 0 ? (
          <p className="text-muted-foreground text-sm">
            No other sessions are signed in.
          </p>
        ) : (
          <ul className="divide-y">
            {others.map((item) => (
              <li
                key={item.id}
                className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm"
              >
                <div className="min-w-0">
                  <p className="truncate font-mono text-xs">
                    {item.user_agent ?? 'Unknown browser'}
                  </p>
                  <p className="text-muted-foreground text-xs">
                    {item.remote_addr ?? 'unknown address'} · signed in{' '}
                    {formatWhen(item.created_at)}
                  </p>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setConfirmId(item.id)}
                  disabled={endSession.isPending}
                >
                  End
                </Button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="space-y-3 rounded-lg border p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="flex items-center gap-2 text-sm font-semibold">
              <History aria-hidden="true" className="size-4" />
              Your activity
            </h2>
            <p className="text-muted-foreground text-xs">
              Recent security-relevant events. Records cannot be edited or deleted.
            </p>
          </div>
          {user ? <Badge variant="outline">@{user.username}</Badge> : null}
        </div>

        {audit.isPending ? (
          <div className="space-y-2">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        ) : audit.error ? (
          <p role="alert" className="text-destructive flex items-center gap-2 text-sm">
            <AlertCircle aria-hidden="true" className="size-4" />
            Could not load your activity.
          </p>
        ) : audit.data && audit.data.events.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-muted-foreground border-b text-left text-xs">
                  <th className="py-2 pr-4 font-medium">When</th>
                  <th className="py-2 pr-4 font-medium">Action</th>
                  <th className="py-2 pr-4 font-medium">Outcome</th>
                  <th className="py-2 pr-4 font-medium">Resource</th>
                  <th className="py-2 font-medium">From</th>
                </tr>
              </thead>
              <tbody>
                {audit.data.events.map((event) => (
                  <tr key={event.id} className="border-b last:border-0">
                    <td className="text-muted-foreground py-2 pr-4 whitespace-nowrap text-xs">
                      {formatWhen(event.created_at)}
                    </td>
                    <td className="py-2 pr-4 font-mono text-xs">{event.action}</td>
                    <td className="py-2 pr-4">
                      <Badge
                        variant={event.outcome === 'success' ? 'outline' : 'destructive'}
                      >
                        {event.outcome}
                      </Badge>
                    </td>
                    <td className="text-muted-foreground py-2 pr-4 text-xs">
                      {event.resource_type
                        ? `${event.resource_type} #${event.resource_id ?? '?'}`
                        : '—'}
                    </td>
                    <td className="text-muted-foreground py-2 font-mono text-xs">
                      {event.remote_addr ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-muted-foreground text-sm">No activity recorded yet.</p>
        )}
      </section>

      <Dialog open={confirmId !== null} onOpenChange={(open) => !open && setConfirmId(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>End this session?</DialogTitle>
            <DialogDescription>
              That browser will have to sign in again. Any edit locks it held are
              released.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmId(null)}>
              Cancel
            </Button>
            <Button
              disabled={endSession.isPending}
              onClick={() => confirmId !== null && void handleEnd(confirmId)}
            >
              {endSession.isPending ? 'Ending…' : 'End session'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
