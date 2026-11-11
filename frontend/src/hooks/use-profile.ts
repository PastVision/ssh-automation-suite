/**
 * SSH profile queries and mutations.
 *
 * Every request here is scoped to the caller by the backend: no function takes a
 * user id, because no route accepts one.
 *
 * Two response shapes matter. `GET`/`PATCH /api/profile` return the whole profile;
 * the secret endpoints return only `SecretState`, so they merge into the cached
 * profile instead of replacing it -- otherwise the connection settings would
 * vanish from the cache after saving a passphrase.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiFetch, jsonInit } from '@/lib/api-client'
import type { ProfileUpdate, SecretState, SshProfile } from '@/lib/types'

const PROFILE_KEY = ['profile'] as const

/** Fold a fresh secret state into the cached profile, when there is one. */
function useSecretStateWriter() {
  const queryClient = useQueryClient()
  return (secrets: SecretState) => {
    queryClient.setQueryData<SshProfile>(PROFILE_KEY, (previous) =>
      previous ? { ...previous, secrets } : previous,
    )
  }
}

export function useProfile() {
  return useQuery({
    queryKey: PROFILE_KEY,
    queryFn: () => apiFetch<SshProfile>('/api/profile'),
    staleTime: 30_000,
  })
}

export function useUpdateProfile() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (update: ProfileUpdate) =>
      apiFetch<SshProfile>('/api/profile', jsonInit(update, 'PATCH')),
    onSuccess: (profile) => {
      queryClient.setQueryData(PROFILE_KEY, profile)
    },
  })
}

export function useUploadPrivateKey() {
  const writeSecrets = useSecretStateWriter()
  return useMutation({
    mutationFn: (input: { private_key: string; passphrase?: string | null }) =>
      apiFetch<SecretState>('/api/profile/private-key', jsonInit(input, 'PUT')),
    onSuccess: writeSecrets,
  })
}

export function useDeletePrivateKey() {
  const writeSecrets = useSecretStateWriter()
  return useMutation({
    mutationFn: () =>
      apiFetch<SecretState>('/api/profile/private-key', { method: 'DELETE' }),
    onSuccess: writeSecrets,
  })
}

export function useSetPassphrase() {
  const writeSecrets = useSecretStateWriter()
  return useMutation({
    mutationFn: (passphrase: string) =>
      apiFetch<SecretState>(
        '/api/profile/key-passphrase',
        jsonInit({ passphrase }, 'PUT'),
      ),
    onSuccess: writeSecrets,
  })
}

export function useClearPassphrase() {
  const writeSecrets = useSecretStateWriter()
  return useMutation({
    mutationFn: () =>
      apiFetch<SecretState>('/api/profile/key-passphrase', { method: 'DELETE' }),
    onSuccess: writeSecrets,
  })
}

export function useSetSudoPassword() {
  const writeSecrets = useSecretStateWriter()
  return useMutation({
    mutationFn: (password: string) =>
      apiFetch<SecretState>(
        '/api/profile/sudo-password',
        jsonInit({ password }, 'PUT'),
      ),
    onSuccess: writeSecrets,
  })
}

export function useClearSudoPassword() {
  const writeSecrets = useSecretStateWriter()
  return useMutation({
    mutationFn: () =>
      apiFetch<SecretState>('/api/profile/sudo-password', { method: 'DELETE' }),
    onSuccess: writeSecrets,
  })
}
