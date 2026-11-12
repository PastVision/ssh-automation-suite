/**
 * Execution history and transcript queries.
 *
 * History is polled while anything is still running. Per-host output is not
 * polled: an open transcript follows a live WebSocket instead (see
 * `use-transcript-stream`). Once the newest run is terminal the interval stops,
 * so a calm history page makes no requests.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiFetch } from '@/lib/api-client'
import type {
  CancelResponse,
  ExecutionDetail,
  ExecutionListResponse,
  ExecutionStatus,
  HostTranscript,
} from '@/lib/types'

const LIST_KEY = ['executions'] as const

/** Statuses a cancellation request can still act on. */
export function isCancellable(status: ExecutionStatus): boolean {
  return status === 'pending' || status === 'running'
}

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

/**
 * Why a cancellation request failed, in terms an operator can act on.
 *
 * A repeated click is not an error: the backend records the request once and
 * answers `cancel_requested: false` while the run is still live, so only a
 * genuine transport or permission failure reaches here.
 */
export function describeCancelError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 404) return 'This run no longer exists.'
    if (error.status === 403) return 'You do not have permission to cancel this run.'
    if (error.status === 0) return 'The request timed out. Check the run state and retry.'
    return error.detail ?? 'Could not cancel this run.'
  }
  return 'Could not cancel this run.'
}

/**
 * Request cancellation for a run.
 *
 * Deliberately does not assume the run stops: the request is cooperative, so the
 * detail is invalidated and the existing poll decides when the status actually
 * changes. Both the detail and the list are refreshed because the list shows the
 * same status.
 */
export function useCancelExecution() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: number) =>
      apiFetch<CancelResponse>(`/api/executions/${id}/cancel`, { method: 'POST' }),
    onSettled: (_data, _error, id) => {
      void queryClient.invalidateQueries({ queryKey: executionKey(id) })
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}
