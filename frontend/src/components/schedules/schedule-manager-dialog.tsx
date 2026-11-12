/**
 * Everything scheduling-related for one workflow, in a single dialog.
 *
 * The list and the editor share this container so that closing the editor
 * returns to a list that has already been invalidated by the mutation -- there
 * is no path where the two disagree about what exists.
 */

import { Plus } from 'lucide-react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import type { Schedule } from '@/lib/types'

import { ScheduleDialog } from './schedule-dialog'
import { ScheduleList } from './schedule-list'

export function ScheduleManagerDialog({
  open,
  onOpenChange,
  workflowId,
  workflowName,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  workflowId: number
  workflowName: string
}) {
  const [editing, setEditing] = useState<Schedule | null>(null)
  const [creating, setCreating] = useState(false)

  function close() {
    setEditing(null)
    setCreating(false)
    onOpenChange(false)
  }

  return (
    <>
      <Dialog open={open} onOpenChange={(next) => (next ? undefined : close())}>
        <DialogContent className="max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Schedules</DialogTitle>
            <DialogDescription>
              Timetables for <span className="font-medium">{workflowName}</span>.
              A run started by a schedule is recorded in Executions like any
              other.
            </DialogDescription>
          </DialogHeader>

          <div className="flex justify-end">
            <Button size="sm" onClick={() => setCreating(true)}>
              <Plus /> New schedule
            </Button>
          </div>

          <ScheduleList workflowId={workflowId} onEdit={setEditing} />
        </DialogContent>
      </Dialog>

      {/* Mounted only while open so the form's state starts from the schedule
          it was opened with, rather than persisting across edits. */}
      {creating ? (
        <ScheduleDialog
          open
          onOpenChange={(next) => (next ? undefined : setCreating(false))}
          workflowId={workflowId}
          workflowName={workflowName}
        />
      ) : null}

      {editing ? (
        <ScheduleDialog
          open
          onOpenChange={(next) => (next ? undefined : setEditing(null))}
          workflowId={workflowId}
          workflowName={workflowName}
          schedule={editing}
        />
      ) : null}
    </>
  )
}
