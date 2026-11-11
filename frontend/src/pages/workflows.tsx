/**
 * Workflows page.
 *
 * Lists the workflows the caller can see, opens the editor for create/edit, and
 * launches a run whose progress is shown inline by `RunMonitor`.
 *
 * The list holds no workflow body: the editor receives the loaded detail and the
 * page refetches after a save, so a card can never describe a workflow that no
 * longer matches the backend.
 */

import { GitBranch, Pencil, Play, Plus, RefreshCw, Search, Trash2, Users } from 'lucide-react'
import { useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { RunMonitor } from '@/components/workflows/run-monitor'
import { Notice, WorkflowEditor } from '@/components/workflows/workflow-editor'
import {
  useCreateWorkflow,
  useDeleteWorkflow,
  useRunWorkflow,
  useUpdateWorkflow,
  useWorkflow,
  useWorkflows,
} from '@/hooks/use-workflows'
import type { WorkflowCreate, WorkflowSummary, WorkflowUpdate } from '@/lib/types'

export function WorkflowsPage() {
  const [search, setSearch] = useState('')
  const [editingId, setEditingId] = useState<number | 'new' | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [runningId, setRunningId] = useState<number | null>(null)

  const list = useWorkflows(search)
  const editing = useWorkflow(typeof editingId === 'number' ? editingId : null)

  const create = useCreateWorkflow()
  const update = useUpdateWorkflow(typeof editingId === 'number' ? editingId : 0)
  const remove = useDeleteWorkflow()
  const run = useRunWorkflow()

  async function handleCreate(payload: WorkflowCreate) {
    const created = await create.mutateAsync(payload)
    setEditingId(null)
    setNotice(`Created "${created.name}".`)
  }

  async function handleSave(payload: WorkflowUpdate, rowVersion: number) {
    const saved = await update.mutateAsync({ ...payload, row_version: rowVersion })
    setEditingId(null)
    setNotice(`Saved "${saved.name}" at revision ${saved.row_version}.`)
  }

  async function handleDelete(workflow: WorkflowSummary) {
    const confirmed = window.confirm(
      `Delete "${workflow.name}"? Past executions keep their own snapshots and logs.`,
    )
    if (!confirmed) return
    try {
      await remove.mutateAsync(workflow.id)
      setNotice(`Deleted "${workflow.name}".`)
    } catch (error) {
      const detail =
        (error as { detail?: string | null }).detail ??
        'Could not delete this workflow.'
      setNotice(detail)
    }
  }

  async function handleRun(workflow: WorkflowSummary) {
    try {
      const accepted = await run.mutateAsync({ id: workflow.id })
      setRunningId(accepted.execution_id)
      setNotice(`Started run #${accepted.execution_id} for "${workflow.name}".`)
    } catch (error) {
      const detail =
        (error as { detail?: string | null }).detail ??
        'Could not start this workflow.'
      setNotice(detail)
    }
  }

  const editingWorkflow = editingId === 'new' ? null : (editing.data ?? null)

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Workflows</h1>
          <p className="text-muted-foreground text-sm">
            Ordered template sequences executed across a list of hosts. Steps run
            sequentially per host and stop that host on the first failure.
          </p>
        </div>
        <Button onClick={() => setEditingId('new')}>
          <Plus /> New workflow
        </Button>
      </div>

      {notice && (
        <Notice tone="muted">
          <span className="flex items-start justify-between gap-3">
            <span>{notice}</span>
            <button
              type="button"
              className="text-xs underline underline-offset-2"
              onClick={() => setNotice(null)}
            >
              dismiss
            </button>
          </span>
        </Notice>
      )}

      {runningId !== null && (
        <RunMonitor executionId={runningId} onDismiss={() => setRunningId(null)} />
      )}

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 sm:max-w-xs">
          <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2" />
          <input
            className="bg-background focus-visible:ring-ring/50 w-full rounded-md border py-1.5 pr-2.5 pl-8 text-sm outline-none focus-visible:ring-[3px]"
            placeholder="Search name or description"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            aria-label="Search workflows"
          />
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => void list.refetch()}
          disabled={list.isFetching}
        >
          <RefreshCw className={list.isFetching ? 'size-4 animate-spin' : 'size-4'} />
          Refresh
        </Button>
        <span className="text-muted-foreground ml-auto text-xs">
          {list.data
            ? `${list.data.items.length} workflow${list.data.items.length === 1 ? '' : 's'}`
            : '—'}
        </span>
      </div>

      {list.isLoading && (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((key) => (
            <Skeleton key={key} className="h-44 w-full" />
          ))}
        </div>
      )}

      {list.isError && (
        <Notice tone="danger">Could not load workflows. Try refreshing the page.</Notice>
      )}

      {list.data && list.data.items.length === 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <GitBranch className="size-4" /> No workflows yet
            </CardTitle>
            <CardDescription>
              A workflow combines templates, a host list and optional Run-As blocks.
              Create one to start running it against hosts.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button onClick={() => setEditingId('new')}>
              <Plus /> New workflow
            </Button>
          </CardContent>
        </Card>
      )}

      {list.data && list.data.items.length > 0 && (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {list.data.items.map((workflow) => (
            <WorkflowCard
              key={workflow.id}
              workflow={workflow}
              onEdit={() => setEditingId(workflow.id)}
              onDelete={() => void handleDelete(workflow)}
              onRun={() => void handleRun(workflow)}
              running={run.isPending && run.variables?.id === workflow.id}
            />
          ))}
        </div>
      )}

      <WorkflowEditor
        open={editingId !== null}
        workflow={editingId === 'new' ? null : editingWorkflow}
        onOpenChange={(open) => {
          if (!open) setEditingId(null)
        }}
        onCreate={handleCreate}
        onSave={handleSave}
        onReload={async () => {
          if (typeof editingId === 'number') await editing.refetch()
        }}
        saving={create.isPending || update.isPending}
        error={create.error ?? update.error}
      />
    </div>
  )
}

interface WorkflowCardProps {
  workflow: WorkflowSummary
  onEdit: () => void
  onDelete: () => void
  onRun: () => void
  running: boolean
}

function WorkflowCard({ workflow, onEdit, onDelete, onRun, running }: WorkflowCardProps) {
  return (
    <Card className="flex flex-col">
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="text-base leading-tight">{workflow.name}</CardTitle>
          {!workflow.is_owner && (
            <Badge variant="outline" className="shrink-0">
              <Users className="size-3" /> shared
            </Badge>
          )}
        </div>
        <CardDescription className="line-clamp-2 min-h-8">
          {workflow.description || 'No description'}
        </CardDescription>
      </CardHeader>

      <CardContent className="flex flex-1 flex-col justify-between gap-3">
        <div className="text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
          <span>
            {workflow.step_count} step{workflow.step_count === 1 ? '' : 's'}
          </span>
          {workflow.run_as_count > 0 && <span>{workflow.run_as_count} Run-As</span>}
          <span>
            {workflow.variable_count} variable{workflow.variable_count === 1 ? '' : 's'}
          </span>
          <span>
            {workflow.host_count} host{workflow.host_count === 1 ? '' : 's'}
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          <Button
            size="sm"
            onClick={onRun}
            disabled={running || workflow.host_count === 0}
            title={
              workflow.host_count === 0
                ? 'Add at least one host before running'
                : 'Run this workflow now'
            }
          >
            <Play /> {running ? 'Starting…' : 'Run'}
          </Button>
          <Button variant="outline" size="sm" onClick={onEdit}>
            <Pencil /> {workflow.can_edit ? 'Edit' : 'View'}
          </Button>
          {workflow.is_owner && (
            <Button
              variant="ghost"
              size="sm"
              className="text-destructive hover:text-destructive ml-auto"
              onClick={onDelete}
              title="Delete workflow"
            >
              <Trash2 />
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
