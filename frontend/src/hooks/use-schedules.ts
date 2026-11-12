/**
 * Schedule queries and mutations.
 *
 * Two behaviours are deliberate and worth knowing before editing:
 *
 * - Replacing a definition and toggling `enabled` are guarded by `row_version`,
 *   so a stale tab gets a 409 instead of overwriting a change made elsewhere.
 *   The caller passes the revision it last saw; this module never caches one
 *   itself. Deleting takes no revision -- the endpoint accepts none.
 * - No timing is computed here. The next firing comes from `next_run_at` and
 *   the preview from `/schedules/preview`, because a second cron
 *   implementation in the browser would disagree with the one that dispatches.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiFetch, jsonInit } from '@/lib/api-client'
import type {
  Schedule,
  ScheduleInput,
  ScheduleKind,
  ScheduleListResponse,
  SchedulePreviewResponse,
  ScheduleUpdate,
} from '@/lib/types'

const LIST_KEY = ['schedules'] as const

/**
 * Schedules visible to the caller, newest first. Passing a workflow scopes the
 * list to that workflow; omitting it is what the dashboard uses.
 */
export function useSchedules(workflowId?: number | null) {
  return useQuery({
    queryKey: [...LIST_KEY, workflowId ?? 'all'],
    queryFn: () =>
      apiFetch<ScheduleListResponse>(
        workflowId ? `/api/schedules?workflow_id=${workflowId}` : '/api/schedules',
      ),
    staleTime: 10_000,
  })
}

export function useCreateSchedule() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: ScheduleInput) =>
      apiFetch<Schedule>('/api/schedules', jsonInit(payload)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}

export function useUpdateSchedule(id: number) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: ScheduleUpdate) =>
      apiFetch<Schedule>(`/api/schedules/${id}`, jsonInit(payload, 'PUT')),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}

/**
 * Pause or resume. Separate from `useUpdateSchedule` because the endpoint takes
 * only a boolean and a revision -- sending a whole definition would invite a
 * caller to change timing as a side effect of a toggle.
 */
export function useSetScheduleEnabled() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: { id: number; enabled: boolean; row_version: number }) =>
      apiFetch<Schedule>(
        `/api/schedules/${input.id}/enabled`,
        jsonInit({ enabled: input.enabled, row_version: input.row_version }),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}

export function useDeleteSchedule() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: number) =>
      apiFetch<null>(`/api/schedules/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: LIST_KEY })
    },
  })
}

/**
 * Preview the next firings of a timing definition the user is still editing.
 *
 * Nothing is stored by this call. It is enabled only when the caller has
 * something previewable, so an empty new form does not fire a request that is
 * guaranteed to fail validation.
 */
export function useSchedulePreview(input: {
  schedule_kind: ScheduleKind
  timezone: string
  cron_expression?: string | null
  run_at?: string | null
  enabled: boolean
}) {
  const { enabled, ...body } = input
  return useQuery({
    queryKey: ['schedules', 'preview', body],
    queryFn: () =>
      apiFetch<SchedulePreviewResponse>(
        '/api/schedules/preview',
        jsonInit({ ...body, count: 5 }),
      ),
    enabled,
    // A preview is a pure function of the body: no reason to refetch it.
    staleTime: Infinity,
    retry: false,
  })
}
