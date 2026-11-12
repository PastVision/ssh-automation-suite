import { AlertTriangle, CheckCircle2, RefreshCw, XCircle } from 'lucide-react'
import { Link } from 'react-router-dom'

import { useHealth } from '@/hooks/use-health'
import { ApiError } from '@/lib/api-client'
import { NAV_ITEMS } from '@/components/layout/nav-items'
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

function StatusPill({ status }: { status: 'ok' | 'degraded' }) {
  return status === 'ok' ? (
    <Badge variant="success" className="gap-1.5">
      <CheckCircle2 aria-hidden="true" />
      Healthy
    </Badge>
  ) : (
    <Badge variant="warning" className="gap-1.5">
      <AlertTriangle aria-hidden="true" />
      Degraded
    </Badge>
  )
}

export function DashboardPage() {
  const { data, error, isPending, isFetching, refetch } = useHealth()

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader className="flex-row items-start justify-between gap-4">
          <div className="space-y-1.5">
            <CardTitle className="text-base">System status</CardTitle>
            <CardDescription>
              Live view of the backend foundation — database, storage and the
              concurrency budget.
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            {data && <StatusPill status={data.status} />}
            <Button
              variant="outline"
              size="sm"
              onClick={() => void refetch()}
              disabled={isFetching}
            >
              <RefreshCw aria-hidden="true" className={isFetching ? 'animate-spin' : ''} />
              Refresh
            </Button>
          </div>
        </CardHeader>

        <CardContent>
          {isPending ? (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              {Array.from({ length: 4 }).map((_, index) => (
                <Skeleton key={index} className="h-20 w-full" />
              ))}
            </div>
          ) : error ? (
            <div className="border-destructive/30 bg-destructive/5 flex items-start gap-3 rounded-lg border p-4">
              <XCircle aria-hidden="true" className="text-destructive mt-0.5 size-4" />
              <div className="space-y-1 text-sm">
                <p className="font-medium">Cannot reach the backend</p>
                <p className="text-muted-foreground">
                  {error instanceof ApiError && error.status === 0
                    ? 'The API request timed out. Is the backend running on port 8010?'
                    : error.message}
                </p>
                <code className="text-muted-foreground block pt-1 text-xs">
                  uvicorn app.main:app --port 8010
                </code>
              </div>
            </div>
          ) : (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <Stat
                label="Database"
                value={data.database.ok ? 'Connected' : 'Unavailable'}
                hint={data.database.url_scheme}
                tone={data.database.ok ? 'ok' : 'error'}
              />
              <Stat
                label="Concurrency budget"
                value={`${data.concurrency.available} / ${data.concurrency.limit}`}
                hint="host workflow slots free"
                tone="ok"
              />
              <Stat
                label="Scheduler"
                value={
                  data.scheduler.running
                    ? 'Running'
                    : data.scheduler_enabled
                      ? 'Waiting for lease'
                      : 'Disabled'
                }
                hint={
                  data.scheduler.lease_held
                    ? 'this process dispatches scheduled runs'
                    : 'scheduled runs are not dispatched here'
                }
                tone={data.scheduler.lease_held ? 'ok' : 'neutral'}
              />
              <Stat
                label="Environment"
                value={data.environment}
                hint={`v${data.version}`}
                tone="neutral"
              />
            </div>
          )}
        </CardContent>
      </Card>

      {data && (
        <div className="grid gap-6 lg:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Storage</CardTitle>
              <CardDescription>
                Metadata lives in SQLite; transcripts stay on the filesystem.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <dl className="space-y-3 text-sm">
                <PathRow label="Database" value={data.database.url_scheme} />
                <PathRow label="Data directory" value={data.paths.data_dir} />
                <PathRow label="Execution logs" value={data.paths.execution_log_dir} />
                <PathRow label="Key store" value={data.paths.private_key_dir} />
              </dl>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Areas</CardTitle>
              <CardDescription>
                What each area does today. Scheduling is the only part still
                outstanding.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <ul className="divide-y">
                {NAV_ITEMS.filter((item) => item.to !== '/').map((item) => (
                  <li key={item.to}>
                    <Link
                      to={item.to}
                      className="hover:bg-accent/60 -mx-2 flex items-center justify-between gap-3 rounded-md px-2 py-2.5 transition-colors"
                    >
                      <span className="flex items-center gap-3">
                        <item.icon
                          aria-hidden="true"
                          className="text-muted-foreground size-4"
                        />
                        <span className="text-sm font-medium">{item.label}</span>
                      </span>
                      <Badge variant="success">Ready</Badge>
                    </Link>
                  </li>
                ))}
              </ul>
              <p className="text-muted-foreground mt-3 text-xs">
                Schedules are created from a workflow. Whether this process dispatches
                them is the scheduler status shown above.
              </p>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  )
}

function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string
  value: string
  hint: string
  tone: 'ok' | 'error' | 'neutral'
}) {
  return (
    <div className="bg-muted/40 rounded-lg border p-4">
      <p className="text-muted-foreground text-xs font-medium tracking-wide uppercase">
        {label}
      </p>
      <p className="mt-1.5 flex items-center gap-2 text-lg font-semibold capitalize">
        {value}
        {tone === 'ok' && (
          <CheckCircle2 aria-hidden="true" className="text-success size-4" />
        )}
        {tone === 'error' && (
          <XCircle aria-hidden="true" className="text-destructive size-4" />
        )}
      </p>
      <p className="text-muted-foreground mt-0.5 text-xs">{hint}</p>
    </div>
  )
}

function PathRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-muted-foreground text-xs font-medium tracking-wide uppercase">
        {label}
      </dt>
      <dd className="truncate font-mono text-xs" title={value}>
        {value}
      </dd>
    </div>
  )
}
