/**
 * SSH credential queries and mutations.
 *
 * The private key travels one way only: it is sent on create and never comes
 * back, so nothing here caches or re-reads it. A credential row is identified by
 * name and fingerprint instead.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiFetch, jsonInit } from '@/lib/api-client'
import type {
  Credential,
  CredentialCreate,
  CredentialListResponse,
} from '@/lib/types'

const LIST_KEY = ['credentials'] as const

export function useCredentials() {
  return useQuery({
    queryKey: LIST_KEY,
    queryFn: () => apiFetch<CredentialListResponse>('/api/credentials'),
    staleTime: 15_000,
  })
}

export function useCreateCredential() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: CredentialCreate) =>
      apiFetch<Credential>('/api/credentials', jsonInit(payload)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}

export function useDeleteCredential() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: number) =>
      apiFetch<null>(`/api/credentials/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}
