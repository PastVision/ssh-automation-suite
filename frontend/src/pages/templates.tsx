/**
 * Templates page.
 *
 * Lists every template the caller can see — their own and anything explicitly
 * shared with them — with a lock badge so a conflict is visible before opening
 * the editor instead of after a failed save.
 *
 * The page holds no template content in its own state: the editor dialog receives
 * the loaded detail and refetches after a save, so the list can never drift from
 * the backend's version.
 */

import {
  Copy,
  FileCode2,
  Lock,
  Pencil,
  Plus,
  Search,
  Share2,
  Trash2,
  Users,
} from 'lucide-react'
import { useState } from 'react'

import {
  Notice,
  TemplateEditor,
  type TemplateDraft,
} from '@/components/templates/template-editor'
import { VersionHistory } from '@/components/templates/version-history'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import {
  formatVersion,
  useCreateTemplate,
  useDeleteTemplate,
  useDuplicateTemplate,
  useSetVisibility,
  useTemplate,
  useTemplates,
  useUpdateTemplate,
} from '@/hooks/use-templates'
import type { TemplateDetail, TemplateSummary } from '@/lib/types'
import { formatApiInstant } from '@/lib/display'

export function TemplatesPage() {
  const [search, setSearch] = useState('')
  const [ownedOnly, setOwnedOnly] = useState(false)
  const [editingId, setEditingId] = useState<number | 'new' | null>(null)
  const [historyId, setHistoryId] = useState<number | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const list = useTemplates(search, ownedOnly)
  const editing = useTemplate(typeof editingId === 'number' ? editingId : null)
  const history = useTemplate(historyId)

  const create = useCreateTemplate()
  const duplicate = useDuplicateTemplate()
  const remove = useDeleteTemplate()
  const update = useUpdateTemplate(typeof editingId === 'number' ? editingId : 0)
  const visibility = useSetVisibility()

  async function handleCreate(draft: TemplateDraft) {
    const created = await create.mutateAsync({ ...draft, visibility: 'private' })
    setEditingId(null)
    setNotice(`Created “${created.name}” as v${created.current_version}.`)
  }

  async function handleSave(draft: TemplateDraft, rowVersion: number) {
    const saved = await update.mutateAsync({ ...draft, row_version: rowVersion })
    setEditingId(null)
    setNotice(`Saved “${saved.name}” — now at v${saved.current_version}.`)
  }

  async function handleDuplicate(template: TemplateSummary) {
    const copy = await duplicate.mutateAsync({ id: template.id })
    setNotice(`Created “${copy.name}” as a private copy.`)
  }

  async function handleDelete(template: TemplateSummary) {
    const confirmed = window.confirm(
      `Delete “${template.name}” and its ${template.current_version ?? 0} version(s)? Past execution logs are kept.`,
    )
    if (!confirmed) return
    try {
      await remove.mutateAsync(template.id)
      setNotice(`Deleted “${template.name}”.`)
    } catch (error) {
      const message =
        error instanceof Error && 'detail' in error
          ? String((error as { detail: string | null }).detail ?? error.message)
          : 'Could not delete this template.'
      setNotice(message)
    }
  }

  async function handleVisibility(template: TemplateSummary) {
    const next = template.visibility === 'shared' ? 'private' : 'shared'
    await visibility.mutateAsync({ id: template.id, visibility: next })
    setNotice(
      next === 'private'
        ? `“${template.name}” is private again — existing shares no longer grant access.`
        : `“${template.name}” is shared. Grant access to individual users from the API.`,
    )
  }

  const editingTemplate: TemplateSummary | null =
    editingId === 'new' ? null : (editing.data ?? null)

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Templates</h1>
          <p className="text-sm text-muted-foreground">
            Reusable shell scripts with immutable version history. Every save that
            changes something appends a version.
          </p>
        </div>
        <Button onClick={() => setEditingId('new')}>
          <Plus /> New template
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

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 sm:max-w-xs">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
          <input
            className="w-full rounded-md border bg-background py-1.5 pr-2.5 pl-8 text-sm outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
            placeholder="Search name or description"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            aria-label="Search templates"
          />
        </div>
        <Button
          variant={ownedOnly ? 'secondary' : 'outline'}
          size="sm"
          onClick={() => setOwnedOnly((value) => !value)}
        >
          <Users /> Only mine
        </Button>
        <span className="ml-auto text-xs text-muted-foreground">
          {list.data ? `${list.data.total} template${list.data.total === 1 ? '' : 's'}` : '—'}
        </span>
      </div>

      {list.isLoading && (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((key) => (
            <Skeleton key={key} className="h-40 w-full" />
          ))}
        </div>
      )}

      {list.isError && (
        <Notice tone="danger">Could not load templates. Try refreshing the page.</Notice>
      )}

      {list.data && list.data.templates.length === 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <FileCode2 className="size-4" /> No templates yet
            </CardTitle>
            <CardDescription>
              {ownedOnly
                ? 'You do not own any templates yet.'
                : 'Create a template, or ask a colleague to share one with you.'}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button onClick={() => setEditingId('new')}>
              <Plus /> New template
            </Button>
          </CardContent>
        </Card>
      )}

      {list.data && list.data.templates.length > 0 && (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {list.data.templates.map((template) => (
            <TemplateCard
              key={template.id}
              template={template}
              onEdit={() => setEditingId(template.id)}
              onHistory={() => setHistoryId(template.id)}
              onDuplicate={() => void handleDuplicate(template)}
              onDelete={() => void handleDelete(template)}
              onVisibility={() => void handleVisibility(template)}
            />
          ))}
        </div>
      )}

      <TemplateEditor
        open={editingId !== null}
        template={editingId === 'new' ? null : editingTemplate}
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

      {history.data && (
        <VersionHistory
          open={historyId !== null}
          template={history.data as TemplateDetail}
          onOpenChange={(open) => {
            if (!open) setHistoryId(null)
          }}
        />
      )}
    </div>
  )
}

interface TemplateCardProps {
  template: TemplateSummary
  onEdit: () => void
  onHistory: () => void
  onDuplicate: () => void
  onDelete: () => void
  onVisibility: () => void
}

function TemplateCard({
  template,
  onEdit,
  onHistory,
  onDuplicate,
  onDelete,
  onVisibility,
}: TemplateCardProps) {
  const locked = template.locked_by_other || (template.lock !== null && !template.lock.is_mine)
  const holder = template.lock

  return (
    <Card className="flex flex-col">
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="text-base leading-tight">{template.name}</CardTitle>
          <div className="flex shrink-0 flex-col items-end gap-1">
            <Badge variant={template.visibility === 'shared' ? 'secondary' : 'outline'}>
              {template.visibility === 'shared' ? (
                <>
                  <Share2 className="size-3" /> shared
                </>
              ) : (
                'private'
              )}
            </Badge>
            {!template.is_owner && (
              <Badge variant="outline">{template.owner_label ?? 'shared'}</Badge>
            )}
          </div>
        </div>
        <CardDescription className="line-clamp-2 min-h-8">
          {template.description || 'No description'}
        </CardDescription>
      </CardHeader>

      <CardContent className="flex flex-1 flex-col justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <Badge variant="outline">{formatVersion(template)}</Badge>
          <span>
            {template.step_count} step{template.step_count === 1 ? '' : 's'}
          </span>
          <span>·</span>
          <span>edited {formatApiInstant(template.updated_at)}</span>
          {locked && (
            <span className="ml-auto inline-flex items-center gap-1 text-amber-700 dark:text-amber-300">
              <Lock className="size-3" />
              {holder?.display_name ?? holder?.username} editing
            </span>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          <Button
            variant="outline"
            size="sm"
            onClick={onEdit}
            disabled={!template.can_edit}
            title={template.can_edit ? 'Edit template' : 'You have view-only access'}
          >
            <Pencil /> {template.can_edit ? 'Edit' : 'View'}
          </Button>
          <Button variant="ghost" size="sm" onClick={onHistory}>
            History
          </Button>
          <Button variant="ghost" size="sm" onClick={onDuplicate} title="Copy into your own templates">
            <Copy />
          </Button>
          {template.is_owner && (
            <>
              <Button
                variant="ghost"
                size="sm"
                onClick={onVisibility}
                title={template.visibility === 'shared' ? 'Make private' : 'Share'}
              >
                <Share2 />
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={onDelete}
                title="Delete template"
                className="ml-auto text-destructive hover:text-destructive"
              >
                <Trash2 />
              </Button>
            </>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
