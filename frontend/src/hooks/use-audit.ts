/** The caller's own audit trail. The endpoint is self-scoped; there is no filter. */

import { useQuery } from '@tanstack/react-query'

import { apiFetch } from '@/lib/api-client'
import type { AuditPage } from '@/lib/types'

export function useAuditEvents(limit = 50) {
  return useQuery({
    queryKey: ['audit', limit],
    queryFn: () => apiFetch<AuditPage>(`/api/audit?limit=${limit}`),
    staleTime: 10_000,
  })
}
