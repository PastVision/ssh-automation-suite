/**
 * Execution history and transcript queries.
 *
 * History is polled while anything is still running, because the runner has no
 * WebSocket push yet. Once the newest run is terminal the interval stops, so a
 * calm history page makes no requests.
 */

import { useQuery } from '@tanstack/react-query'

import { apiFetch } from '@/lib/api-client'
import type {
  ExecutionDetail,
  ExecutionListResponse,
  ExecutionStatus,
  HostTranscript,
} from '@/lib/types'

const LIST_KEY = ['executions'] as const

export function executionKey(id: number) {
  return ['executions', id] as const
}

export function useExecutions(statusFilter: ExecutionStatus | 'all') {
  return useQuery({
    queryKey: [...LIST_KEY, statusFilter],
    queryFn: () =>
      apiFetch<ExecutionListResponse>(
        statusFilter === 'all'
          ? '/api/executions?limit=100'
          : `/api/executions?limit=100&status=${statusFilter}`,
      ),
    staleTime: 5_000,
    refetchInterval: (query) => {
      const active = query.state.data?.items.some(
        (item) => item.status === 'pending' || item.status === 'running',
      )
      return active ? 4_000 : false
    },
  })
}

export function useExecution(id: number | null) {
  return useQuery({
    queryKey: executionKey(id ?? -1),
    queryFn: () => apiFetch<ExecutionDetail>(`/api/executions/${id}`),
    enabled: id !== null,
    staleTime: 3_000,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status === 'pending' || status === 'running' ? 4_000 : false
    },
  })
}

export function useHostTranscript(executionId: number, hostRunId: number | null) {
  return useQuery({
    queryKey: ['executions', executionId, 'transcript', hostRunId ?? -1],
    queryFn: () =>
      apiFetch<HostTranscript>(
        `/api/executions/${executionId}/hosts/${hostRunId}/transcript`,
      ),
    enabled: hostRunId !== null,
    staleTime: 2_000,
  })
}
