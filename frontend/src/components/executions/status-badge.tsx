/** Status pill shared by the execution list and the run detail view. */

import { Badge } from '@/components/ui/badge'
import type { ExecutionStatus, HostRunStatus, StepRunStatus } from '@/lib/types'

type AnyStatus = ExecutionStatus | HostRunStatus | StepRunStatus

const VARIANT: Record<
  AnyStatus,
  'default' | 'secondary' | 'destructive' | 'outline' | 'success' | 'warning'
> = {
  pending: 'outline',
  running: 'warning',
  succeeded: 'success',
  failed: 'destructive',
  cancelled: 'secondary',
  skipped: 'secondary',
}

export function StatusBadge({ status }: { status: AnyStatus }) {
  return <Badge variant={VARIANT[status]}>{status}</Badge>
}
