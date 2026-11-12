/** Status pill shared by the execution list and the run detail view. */

import {
  Ban,
  CircleCheck,
  CircleDashed,
  CircleSlash,
  CircleX,
  LoaderCircle,
  type LucideIcon,
} from 'lucide-react'

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

/**
 * Each status also gets a distinct glyph, so the pill survives a viewer who
 * cannot separate the hues. The word stays the label, so the icon is decorative.
 */
const ICON: Record<AnyStatus, LucideIcon> = {
  pending: CircleDashed,
  running: LoaderCircle,
  succeeded: CircleCheck,
  failed: CircleX,
  cancelled: Ban,
  skipped: CircleSlash,
}

export function StatusBadge({ status }: { status: AnyStatus }) {
  const Icon = ICON[status]
  return (
    <Badge variant={VARIANT[status]}>
      <Icon aria-hidden="true" className={status === 'running' ? 'animate-spin' : undefined} />
      {status}
    </Badge>
  )
}
