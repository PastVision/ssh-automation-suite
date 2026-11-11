/**
 * Workflow queries and mutations.
 *
 * A workflow save is refused with `423` unless this session holds the edit lock,
 * exactly like a template save, so `useWorkflowLock` mirrors `useEditorLock`.
 * The two are separate hooks rather than one generic one because the lock
 * endpoints take a resource type and the caller must not be able to pass the
 * wrong one by accident.
 *
 * Launching a run is a mutation, but it deliberately does not invalidate the
 * workflow cache: a run snapshots the workflow and must never make the editor
 * look stale mid-edit. The Executions list owns its own polling.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError, apiFetch, jsonInit } from '@/lib/api-client'
import type {
  LockHolder,
  LockState,
  WorkflowCreate,
  WorkflowDetail,
  WorkflowListResponse,
  WorkflowRunResponse,
  WorkflowUpdate,
} from '@/lib/types'

const LIST_KEY = ['workflows'] as const

export function workflowKey(id: number) {
  return ['workflows', id] as const
}

const RENEW_INTERVAL_MS = 2 * 60 * 1000

export function useWorkflows(search: string) {
  const params = new URLSearchParams()
  if (search.trim()) params.set('search', search.trim())
  const suffix = params.toString() ? `?${params.toString()}` : ''

  return useQuery({
    queryKey: [...LIST_KEY, search.trim()],
    queryFn: () => apiFetch<WorkflowListResponse>(`/api/workflows${suffix}`),
    staleTime: 10_000,
  })
}

export function useWorkflow(id: number | null) {
  return useQuery({
    queryKey: workflowKey(id ?? -1),
    queryFn: () => apiFetch<WorkflowDetail>(`/api/workflows/${id}`),
    enabled: id !== null,
    staleTime: 5_000,
  })
}

export function useCreateWorkflow() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: WorkflowCreate) =>
      apiFetch<WorkflowDetail>('/api/workflows', jsonInit(payload)),
    onSuccess: (created) => {
      queryClient.setQueryData(workflowKey(created.id), created)
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}

export function useUpdateWorkflow(id: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: WorkflowUpdate) =>
      apiFetch<WorkflowDetail>(`/api/workflows/${id}`, jsonInit(payload, 'PUT')),
    onSuccess: (updated) => {
      queryClient.setQueryData(workflowKey(id), updated)
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}

export function useDeleteWorkflow() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: number) =>
      apiFetch<null>(`/api/workflows/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}

/**
 * Start a run. `hostnames` narrows the run to a subset, which is how the UI
 * re-runs only the hosts that failed.
 */
export function useRunWorkflow() {
  return useMutation({
    mutationFn: (input: { id: number; hostnames?: string[] | null }) =>
      apiFetch<WorkflowRunResponse>(
        `/api/workflows/${input.id}/run`,
        jsonInit(input.hostnames ? { hostnames: input.hostnames } : {}),
      ),
  })
}

/* -------------------------------------------------------------------------- */
/* Edit lock                                                                   */
/* -------------------------------------------------------------------------- */

export interface WorkflowLockState {
  held: LockHolder | null
  blockedBy: LockHolder | null
  acquiring: boolean
  error: string | null
  acquire: () => Promise<void>
  release: () => Promise<void>
}

/**
 * Hold the edit lock for one workflow while its editor is open.
 *
 * A workflow that has never been saved has no id yet, so `id` is null while
 * creating: there is nothing to lock and nothing to conflict with.
 */
export function useWorkflowLock(id: number | null, enabled: boolean): WorkflowLockState {
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
    if (id === null) return
    setAcquiring(true)
    setError(null)
    setBlockedBy(null)
    try {
      const state = await apiFetch<LockState>(`/api/locks/workflow/${id}`, {
        method: 'POST',
      })
      setHeld(state.lock)
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        const body = cause.body as
          | { detail?: { lock?: LockHolder; message?: string } }
          | null
        setBlockedBy(body?.detail?.lock ?? null)
        setError(body?.detail?.message ?? 'Another editor holds this workflow.')
      } else if (cause instanceof ApiError && cause.status === 403) {
        setError('You do not have permission to edit this workflow.')
      } else {
        setError('Could not start editing. Try again.')
      }
    } finally {
      setAcquiring(false)
    }
  }, [id])

  const release = useCallback(async () => {
    clearTimer()
    setHeld(null)
    if (id === null) return
    try {
      await apiFetch<null>(`/api/locks/workflow/${id}`, { method: 'DELETE' })
    } catch {
      // Already released or expired: nothing left to do.
    }
  }, [clearTimer, id])

  useEffect(() => {
    if (enabled && id !== null) {
      void acquire()
    }
    return () => {
      clearTimer()
    }
  }, [enabled, id, acquire, clearTimer])

  useEffect(() => {
    if (!held || id === null) return
    timer.current = window.setInterval(() => {
      void apiFetch(`/api/locks/workflow/${id}/renew`, { method: 'PUT' }).catch(() =>
        setHeld(null),
      )
    }, RENEW_INTERVAL_MS)
    return () => clearTimer()
  }, [held, id, clearTimer])

  const heldRef = useRef(false)
  heldRef.current = held !== null
  useEffect(() => {
    return () => {
      if (id !== null && heldRef.current) {
        void apiFetch<null>(`/api/locks/workflow/${id}`, {
          method: 'DELETE',
        }).catch(() => undefined)
      }
    }
  }, [id])

  return { held, blockedBy, acquiring, error, acquire, release }
}
