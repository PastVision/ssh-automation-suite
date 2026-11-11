/** The caller's own sessions and security activity. */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiFetch } from '@/lib/api-client'
import type { SessionInfo } from '@/lib/types'

export function useSessions() {
  return useQuery({
    queryKey: ['sessions'],
    queryFn: () => apiFetch<SessionInfo[]>('/api/auth/sessions'),
    staleTime: 15_000,
  })
}

export function useEndSession() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (sessionId: number) =>
      apiFetch<null>(`/api/auth/sessions/${sessionId}`, { method: 'DELETE' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['sessions'] })
      void queryClient.invalidateQueries({ queryKey: ['audit'] })
    },
  })
}
