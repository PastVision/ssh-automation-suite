import { useQuery } from '@tanstack/react-query'

import { apiFetch } from '@/lib/api-client'
import type { HealthResponse } from '@/lib/types'

export function useHealth() {
  return useQuery({
    queryKey: ['health'],
    queryFn: () => apiFetch<HealthResponse>('/api/health'),
    refetchInterval: 15_000,
    retry: 1,
  })
}
