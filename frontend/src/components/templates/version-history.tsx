/**
 * Version history and a line-level diff between two versions.
 *
 * The diff is deliberately simple: a longest-common-subsequence walk over lines
 * per step, which is exact for the size of script a template holds and needs no
 * dependency. Steps are matched by position, so a reorder shows up as changed
 * steps rather than as a move — that is honest about what a snapshot records,
 * since a version stores an ordered list and nothing about intent.
 */

import { useMemo, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Notice } from '@/components/templates/template-editor'
import { useVersion } from '@/hooks/use-templates'
import type { TemplateDetail, TemplateVersionSummary } from '@/lib/types'
import { cn } from '@/lib/utils'

type DiffOp = { kind: 'same' | 'added' | 'removed'; text: string }

/** LCS line diff. Quadratic in line count, which is fine for shell scripts. */
export function diffLines(before: string[], after: string[]): DiffOp[] {
  const rows = before.length
  const cols = after.length
  const table: number[][] = Array.from({ length: rows + 1 }, () =>
    new Array<number>(cols + 1).fill(0),
  )
  for (let i = rows - 1; i >= 0; i -= 1) {
    for (let j = cols - 1; j >= 0; j -= 1) {
      table[i][j] =
        before[i] === after[j]
          ? table[i + 1][j + 1] + 1
          : Math.max(table[i + 1][j], table[i][j + 1])
    }
  }

  const ops: DiffOp[] = []
  let i = 0
  let j = 0
  while (i < rows && j < cols) {
    if (before[i] === after[j]) {
      ops.push({ kind: 'same', text: before[i] })
      i += 1
      j += 1
    } else if (table[i + 1][j] >= table[i][j + 1]) {
      ops.push({ kind: 'removed', text: before[i] })
      i += 1
    } else {
      ops.push({ kind: 'added', text: after[j] })
      j += 1
    }
  }
  while (i < rows) {
    ops.push({ kind: 'removed', text: before[i] })
    i += 1
  }
  while (j < cols) {
    ops.push({ kind: 'added', text: after[j] })
    j += 1
  }
  return ops
}

function DiffColumn({ ops }: { ops: DiffOp[] }) {
  return (
    <pre className="overflow-x-auto rounded-md border bg-muted/30 p-2 font-mono text-xs leading-relaxed">
      {ops.map((op, index) => (
        <div
          key={index}
          className={cn(
            'whitespace-pre',
            op.kind === 'added' && 'bg-emerald-500/15 text-emerald-900 dark:text-emerald-200',
            op.kind === 'removed' && 'bg-destructive/15 text-destructive',
          )}
        >
          <span className="select-none opacity-50">
            {op.kind === 'added' ? '+ ' : op.kind === 'removed' ? '- ' : '  '}
          </span>
          {op.text || ' '}
        </div>
      ))}
    </pre>
  )
}

interface VersionHistoryProps {
  open: boolean
  template: TemplateDetail
  onOpenChange: (open: boolean) => void
}

export function VersionHistory({ open, template, onOpenChange }: VersionHistoryProps) {
  const [selected, setSelected] = useState<number | null>(null)
  const [compareTo, setCompareTo] = useState<number | null>(null)

  const current = useVersion(template.id, selected)
  const previous = useVersion(template.id, compareTo)

  const diff = useMemo(() => {
    if (!current.data || !previous.data) return null
    const stepCount = Math.max(current.data.steps.length, previous.data.steps.length)
    return Array.from({ length: stepCount }, (_, index) => {
      const before = previous.data.steps[index]
      const after = current.data.steps[index]
      return {
        index,
        beforeName: before?.name ?? null,
        afterName: after?.name ?? null,
        ops: diffLines((before?.script ?? '').split('\n'), (after?.script ?? '').split('\n')),
      }
    })
  }, [current.data, previous.data])

  function pick(version: TemplateVersionSummary) {
    setSelected(version.version)
    const earlier = template.versions.find((v) => v.version === version.version - 1)
    setCompareTo(earlier?.version ?? null)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-5xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Version history — {template.name}</DialogTitle>
          <DialogDescription>
            Every version is immutable. Pick one to inspect it and see what changed from
            the previous version.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4 md:grid-cols-[220px_1fr]">
          <ol className="space-y-1">
            {template.versions.map((version) => (
              <li key={version.id}>
                <Button
                  type="button"
                  variant={selected === version.version ? 'secondary' : 'ghost'}
                  className="w-full justify-start gap-2"
                  onClick={() => pick(version)}
                >
                  <Badge variant="outline">v{version.version}</Badge>
                  <span className="truncate text-xs text-muted-foreground">
                    {version.step_count} step{version.step_count === 1 ? '' : 's'}
                  </span>
                  <span className="ml-auto text-xs text-muted-foreground">
                    {new Date(version.created_at).toLocaleDateString()}
                  </span>
                </Button>
              </li>
            ))}
          </ol>

          <div className="space-y-3">
            {selected === null && (
              <Notice tone="muted">Select a version to inspect it.</Notice>
            )}
            {current.isLoading && <Notice tone="muted">Loading version…</Notice>}
            {current.data && (
              <>
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <Badge variant="outline">v{current.data.version}</Badge>
                  <span className="font-medium">{current.data.name}</span>
                  <span className="text-xs text-muted-foreground">
                    checksum {current.data.content_checksum.slice(0, 12)}…
                  </span>
                  {compareTo !== null && (
                    <span className="ml-auto text-xs text-muted-foreground">
                      compared with v{compareTo}
                    </span>
                  )}
                </div>

                {compareTo === null && (
                  <Notice tone="muted">
                    This is the first version, so there is nothing to compare against.
                  </Notice>
                )}

                {diff ? (
                  <div className="space-y-3">
                    {diff.map((entry) => (
                      <div key={entry.index} className="space-y-1">
                        <div className="text-xs font-medium">
                          Step {entry.index + 1}
                          {entry.afterName && (
                            <span className="ml-2 text-muted-foreground">
                              {entry.beforeName !== entry.afterName && entry.beforeName
                                ? `${entry.beforeName} → ${entry.afterName}`
                                : entry.afterName}
                            </span>
                          )}
                        </div>
                        <DiffColumn ops={entry.ops} />
                      </div>
                    ))}
                  </div>
                ) : (
                  current.data && (
                    <div className="space-y-3">
                      {current.data.steps.map((step) => (
                        <div key={step.position} className="space-y-1">
                          <div className="text-xs font-medium">
                            Step {step.position + 1}
                            <span className="ml-2 text-muted-foreground">{step.name}</span>
                          </div>
                          <pre className="overflow-x-auto rounded-md border bg-muted/30 p-2 font-mono text-xs leading-relaxed whitespace-pre">
                            {step.script}
                          </pre>
                        </div>
                      ))}
                    </div>
                  )
                )}
              </>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
