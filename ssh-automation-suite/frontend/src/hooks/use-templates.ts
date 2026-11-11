/**
 * Template queries and mutations.
 *
 * No function takes a user id: the backend scopes every request to the caller's
 * identity, so an id here could only be used to attempt something that would be
 * rejected anyway.
 *
 * The edit lock is modelled explicitly because a template save is only accepted
 * while the caller holds it. `useTemplateEditor` takes the lock, renews it on a
 * timer, and releases it on unmount, so a component cannot forget to.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError, apiFetch, jsonInit } from '@/lib/api-client'
import type {
  LockHolder,
  LockState,
  TemplateCreate,
  TemplateDetail,
  TemplateListResponse,
  TemplateSummary,
  TemplateUpdate,
  TemplateVersionDetail,
  TemplateVersionSummary,
} from '@/lib/types'

const LIST_KEY = ['templates'] as const

export function templateKey(id: number) {
  return ['templates', id] as const
}

/** Locks expire after the backend's TTL; renew well before that. */
const RENEW_INTERVAL_MS = 2 * 60 * 1000

export function useTemplates(search: string, ownedOnly: boolean) {
  const params = new URLSearchParams()
  if (search.trim()) params.set('search', search.trim())
  if (ownedOnly) params.set('owned', 'true')
  const suffix = params.toString() ? `?${params.toString()}` : ''

  return useQuery({
    queryKey: [...LIST_KEY, search.trim(), ownedOnly],
    queryFn: () => apiFetch<TemplateListResponse>(`/api/templates${suffix}`),
    staleTime: 10_000,
  })
}

export function useTemplate(id: number | null) {
  return useQuery({
    queryKey: templateKey(id ?? -1),
    queryFn: () => apiFetch<TemplateDetail>(`/api/templates/${id}`),
    enabled: id !== null,
    staleTime: 5_000,
  })
}

export function useCreateTemplate() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: TemplateCreate) =>
      apiFetch<TemplateDetail>('/api/templates', jsonInit(payload)),
    onSuccess: (created) => {
      queryClient.setQueryData(templateKey(created.id), created)
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}

export function useUpdateTemplate(id: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: TemplateUpdate) =>
      apiFetch<TemplateDetail>(`/api/templates/${id}`, jsonInit(payload, 'PUT')),
    onSuccess: (updated) => {
      queryClient.setQueryData(templateKey(id), updated)
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}

export function useDeleteTemplate() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: number) =>
      apiFetch<null>(`/api/templates/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}

export function useDuplicateTemplate() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; name?: string }) =>
      apiFetch<TemplateDetail>(
        `/api/templates/${input.id}/duplicate`,
        jsonInit(input.name ? { name: input.name } : {}),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}

export function useSetVisibility() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; visibility: 'private' | 'shared' }) =>
      apiFetch<TemplateDetail>(
        `/api/templates/${input.id}/visibility`,
        jsonInit({ visibility: input.visibility }, 'PATCH'),
      ),
    onSuccess: (updated) => {
      queryClient.setQueryData(templateKey(updated.id), updated)
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}

export function useGrantShare(id: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: { user_id: number; level: 'view' | 'edit' }) =>
      apiFetch(`/api/templates/${id}/shares`, jsonInit(input)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: templateKey(id) })
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}

export function useRevokeShare(id: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (userId: number) =>
      apiFetch<null>(`/api/templates/${id}/shares/${userId}`, { method: 'DELETE' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: templateKey(id) })
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}

export function useVersion(id: number, version: number | null) {
  return useQuery({
    queryKey: ['templates', id, 'versions', version],
    queryFn: () =>
      apiFetch<TemplateVersionDetail>(`/api/templates/${id}/versions/${version}`),
    enabled: version !== null,
  })
}

/* -------------------------------------------------------------------------- */
/* Edit lock                                                                   */
/* -------------------------------------------------------------------------- */

export interface EditorLockState {
  /** The lock this browser session holds, if any. */
  held: LockHolder | null
  /** Somebody else holds it; `held` is null and saving is impossible. */
  blockedBy: LockHolder | null
  acquiring: boolean
  error: string | null
  acquire: () => Promise<void>
  release: () => Promise<void>
}

/**
 * Take and hold the edit lock for one template.
 *
 * Renewal runs on an interval so an editor left open past the lease keeps working,
 * and release happens on unmount so closing the dialog does not block the next
 * person for the rest of the TTL. `release()` is idempotent: the backend answers
 * 404 when nothing is held, which is the expected outcome of a double release.
 */
export function useEditorLock(templateId: number | null, enabled: boolean): EditorLockState {
  const [held, setHeld] = useState<LockHolder | null>(null)
  const [blockedBy, setBlockedBy] = useState<LockHolder | null>(null)
  const [acquiring, setAcquiring] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const timer = useRef<number | null>(null)

  const clearTimer = useCallback(() => {
    if (timer.current !== null) {
      window.clearInterval(timer.current)
      timer.current = null
    }
  }, [])

  const acquire = useCallback(async () => {
    if (templateId === null) return
    setAcquiring(true)
    setError(null)
    setBlockedBy(null)
    try {
      const state = await apiFetch<LockState>(`/api/locks/template/${templateId}`, {
        method: 'POST',
      })
      setHeld(state.lock)
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        const body = cause.body as
          | { detail?: { lock?: LockHolder; message?: string } }
          | null
        setBlockedBy(body?.detail?.lock ?? null)
        setError(body?.detail?.message ?? 'Another editor holds this template.')
      } else if (cause instanceof ApiError && cause.status === 403) {
        setError('You do not have permission to edit this template.')
      } else {
        setError('Could not start editing. Try again.')
      }
    } finally {
      setAcquiring(false)
    }
  }, [templateId])

  const release = useCallback(async () => {
    clearTimer()
    setHeld(null)
    if (templateId === null) return
    try {
      await apiFetch<null>(`/api/locks/template/${templateId}`, { method: 'DELETE' })
    } catch {
      // Already released or expired: nothing left to do.
    }
  }, [clearTimer, templateId])

  // Take the lock as soon as editing is requested.
  useEffect(() => {
    if (enabled && templateId !== null) {
      void acquire()
    }
    return () => {
      clearTimer()
    }
  }, [enabled, templateId, acquire, clearTimer])

  // Renew while held, and stop renewing once the lock is gone.
  useEffect(() => {
    if (!held || templateId === null) return
    timer.current = window.setInterval(() => {
      void apiFetch(`/api/locks/template/${templateId}/renew`, { method: 'PUT' }).catch(
        () => setHeld(null),
      )
    }, RENEW_INTERVAL_MS)
    return () => clearTimer()
  }, [held, templateId, clearTimer])

  // Release on unmount only; a fresh dialog must re-acquire rather than assume.
  const heldRef = useRef(false)
  heldRef.current = held !== null
  useEffect(() => {
    return () => {
      if (templateId !== null && heldRef.current) {
        void apiFetch<null>(`/api/locks/template/${templateId}`, {
          method: 'DELETE',
        }).catch(() => undefined)
      }
    }
  }, [templateId])

  return { held, blockedBy, acquiring, error, acquire, release }
}

/** True when a save failed because somebody else changed the row first. */
export function isStaleUpdate(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409
}

/** True when the save was refused because the lock is not held. */
export function isLockLost(error: unknown): boolean {
  return error instanceof ApiError && error.status === 423
}

export function formatVersion(template: TemplateSummary): string {
  return template.current_version === null ? 'no version' : `v${template.current_version}`
}

export type { TemplateVersionSummary }
