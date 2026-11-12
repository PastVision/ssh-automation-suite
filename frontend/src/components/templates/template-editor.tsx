/**
 * Template editor dialog.
 *
 * The lock is the interesting part. Editing is refused until this session holds
 * it, so the dialog takes it on open, renews it while open, and releases it on
 * close. A 409 on save means somebody saved first: the dialog keeps the user's
 * text and offers a reload instead of silently discarding either version.
 *
 * Steps are an ordered array in local state. Reordering is a move, never a swap
 * of `position` values, because the backend derives positions from array order.
 */

import { AlertCircle, AlertTriangle, ArrowDown, ArrowUp, Info, Plus, Trash2 } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { isLockLost, isStaleUpdate, useEditorLock } from '@/hooks/use-templates'
import type {
  RunAsMethod,
  StepRule,
  StepRuleInput,
  TemplateDetail,
  TemplateStep,
  TemplateSummary,
} from '@/lib/types'
import { cn } from '@/lib/utils'
import { RuleEditor, validateRule } from './rule-editor'

/**
 * A step in the form. `key` is local-only so React keeps identity across moves.
 */
interface DraftStep {
  key: string
  name: string
  description: string
  script: string
  interpreter: string
  timeout_seconds: number
  /** Kept so a save never silently drops a rule the API would otherwise keep. */
  rule: StepRule
  /**
   * Per-step Run-As. Empty means "run as the SSH login user", which is what the
   * backend stores as `null`; there is no way to name a target and then forget
   * the method, because the form always supplies a default.
   */
  run_as_username: string
  run_as_method: RunAsMethod
}

export interface TemplateDraft {
  name: string
  description: string | null
  steps: Array<{
    name: string
    description: string | null
    script: string
    interpreter: string
    timeout_seconds: number
    rule: StepRuleInput
    run_as_username: string | null
    run_as_method: RunAsMethod | null
  }>
}

const INTERPRETERS = ['bash', 'sh', 'python3', 'python', 'perl', 'ruby', 'node'] as const

/**
 * Mirrors `run_as.USERNAME_PATTERN`, including its negative lookbehind: the
 * backend rejects a trailing hyphen, so accepting one here would produce a save
 * the API refuses.
 */
const RUN_AS_TARGET = /^[a-z_][a-z0-9_-]{0,31}$/

let nextKey = 0
function freshKey(): string {
  nextKey += 1
  return `step-${nextKey}`
}

export const CONTINUE_RULE: StepRule = { action: 'continue', condition: null }

function toDrafts(steps: TemplateStep[]): DraftStep[] {
  return steps.map((step) => ({
    key: freshKey(),
    name: step.name,
    description: step.description ?? '',
    script: step.script,
    interpreter: step.interpreter,
    timeout_seconds: step.timeout_seconds,
    rule: step.rule ?? CONTINUE_RULE,
    run_as_username: step.run_as_username ?? '',
    run_as_method: step.run_as_method ?? 'nopasswd',
  }))
}

function emptyStep(): DraftStep {
  nextKey += 1
  return {
    key: `step-${nextKey}`,
    name: `Step ${nextKey}`,
    description: '',
    script: '',
    interpreter: 'bash',
    timeout_seconds: 300,
    rule: CONTINUE_RULE,
    run_as_username: '',
    run_as_method: 'nopasswd',
  }
}

/** A rule is sent in full, or omitted entirely when it is the default. */
function ruleToPayload(rule: StepRule): StepRuleInput {
  if (rule.action === 'continue' || rule.condition === null) {
    return { action: 'continue', condition: null }
  }
  const condition = rule.condition
  return {
    action: rule.action,
    condition: {
      stream: condition.stream,
      location: condition.location,
      operator: condition.operator,
      expected: condition.expected,
      case_sensitive: condition.case_sensitive,
      // Only the field the chosen location uses is sent; the API rejects a
      // condition that carries a value belonging to a different location.
      ...(condition.location === 'line_number'
        ? { line_number: condition.line_number }
        : condition.location === 'anchor'
          ? { anchor: condition.anchor }
          : {}),
    },
  }
}

export function draftToPayload(
  name: string,
  description: string,
  steps: DraftStep[],
): TemplateDraft {
  return {
    name: name.trim(),
    description: description.trim() ? description.trim() : null,
    steps: steps.map((step) => {
      const target = step.run_as_username.trim()
      return {
        name: step.name.trim(),
        description: step.description.trim() ? step.description.trim() : null,
        script: step.script,
        interpreter: step.interpreter.trim(),
        timeout_seconds: Number(step.timeout_seconds) || 0,
        rule: ruleToPayload(step.rule),
        // Both fields travel together or neither does: a target with no method
        // would be rejected, and a method with no target is meaningless.
        run_as_username: target ? target : null,
        run_as_method: target ? step.run_as_method : null,
      }
    }),
  }
}

interface EditorState {
  name: string
  description: string
  steps: DraftStep[]
}

function initialEditor(template: TemplateDetail | null): EditorState {
  if (!template) {
    return { name: '', description: '', steps: [emptyStep()] }
  }
  return {
    name: template.name,
    description: template.description ?? '',
    steps: template.steps.length > 0 ? toDrafts(template.steps) : [emptyStep()],
  }
}

interface TemplateEditorProps {
  open: boolean
  /** `null` creates a new template. */
  template: TemplateSummary | null
  onOpenChange: (open: boolean) => void
  onCreate: (draft: TemplateDraft) => Promise<unknown>
  onSave: (draft: TemplateDraft, rowVersion: number) => Promise<unknown>
  onReload: () => Promise<unknown>
  saving: boolean
  error: unknown
}

export function TemplateEditor({
  open,
  template,
  onOpenChange,
  onCreate,
  onSave,
  onReload,
  saving,
  error,
}: TemplateEditorProps) {
  const isNew = template === null
  const [state, setState] = useState<EditorState>(() => initialEditor(null))
  const [rowVersion, setRowVersion] = useState(1)

  // Only lock an existing template; a new one has no id to lock yet.
  const lock = useEditorLock(template?.id ?? null, open && !isNew)

  const detail = template as TemplateDetail | null

  useEffect(() => {
    if (!open) return
    setState(initialEditor(detail))
    setRowVersion(template?.row_version ?? 1)
  }, [open, template, detail])

  const blocked = !isNew && lock.blockedBy !== null
  const canSave = isNew || lock.held !== null

  const validation = useMemo(() => {
    if (!state.name.trim()) return 'A template needs a name.'
    if (state.steps.length === 0) return 'Add at least one step.'
    for (const [index, step] of state.steps.entries()) {
      if (!step.name.trim()) return `Step ${index + 1} needs a name.`
      if (!step.script.trim()) return `Step ${index + 1} needs a script.`
      if (!Number.isFinite(step.timeout_seconds) || step.timeout_seconds < 1) {
        return `Step ${index + 1} needs a timeout of at least 1 second.`
      }
      // Mirrors `run_as.validate_target` so an unusable target is caught here
      // instead of failing the run after the host was already contacted.
      const target = step.run_as_username.trim()
      if (target && (!RUN_AS_TARGET.test(target) || target.endsWith('-'))) {
        return `Step ${index + 1}: "${target}" is not a valid Run-As username. Use lowercase letters, digits, "_" or "-", starting with a letter or underscore and not ending with "-".`
      }
      if (target === 'root') {
        return `Step ${index + 1}: Run-As cannot target root. Use an unprivileged account with only the sudoers rules it needs.`
      }
      // A partial rule would be rejected by the API; catch it here with a
      // message that names the step instead of surfacing a raw 422.
      const ruleProblem = validateRule(step.rule)
      if (ruleProblem) return `Step ${index + 1}: ${ruleProblem}`
    }
    return null
  }, [state])

  const staleConflict = isStaleUpdate(error)
  const lockLost = isLockLost(error)

  function move(index: number, delta: number) {
    setState((current) => {
      const target = index + delta
      if (target < 0 || target >= current.steps.length) return current
      const steps = [...current.steps]
      const [moved] = steps.splice(index, 1)
      steps.splice(target, 0, moved)
      return { ...current, steps }
    })
  }

  function update(index: number, patch: Partial<DraftStep>) {
    setState((current) => ({
      ...current,
      steps: current.steps.map((step, at) => (at === index ? { ...step, ...patch } : step)),
    }))
  }

  async function submit() {
    const payload = draftToPayload(state.name, state.description, state.steps)
    if (isNew) {
      await onCreate(payload)
    } else {
      await onSave(payload, rowVersion)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-4xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{isNew ? 'New template' : `Edit ${template.name}`}</DialogTitle>
          <DialogDescription>
            {isNew
              ? 'Steps run in order on every host. The first save creates version 1.'
              : `Saving a change appends a new immutable version. Current: v${template.current_version ?? '—'}.`}
          </DialogDescription>
        </DialogHeader>

        {blocked && (
          <Notice tone="warning">
            {lock.blockedBy?.display_name || lock.blockedBy?.username} is editing this
            template. You can read it, but not save until they finish or release it.
          </Notice>
        )}
        {!isNew && !blocked && lock.acquiring && (
          <Notice tone="muted">Taking the edit lock…</Notice>
        )}
        {lock.error && !blocked && <Notice tone="warning">{lock.error}</Notice>}
        {staleConflict && (
          <Notice tone="warning">
            Somebody saved this template while you were editing. Reload to see their
            version, then reapply your changes.
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="ml-3"
              onClick={() => void onReload()}
            >
              Reload
            </Button>
          </Notice>
        )}
        {lockLost && (
          <Notice tone="warning">
            Your edit lock expired or was released. Reopen the editor to continue.
          </Notice>
        )}
        {validation && <Notice tone="muted">{validation}</Notice>}

        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="space-y-1 text-sm">
              <span className="font-medium">Name</span>
              <input
                className={inputClass}
                value={state.name}
                maxLength={120}
                onChange={(event) =>
                  setState((current) => ({ ...current, name: event.target.value }))
                }
                placeholder="Disk usage check"
              />
            </label>
            <label className="space-y-1 text-sm">
              <span className="font-medium">Description</span>
              <input
                className={inputClass}
                value={state.description}
                maxLength={4000}
                onChange={(event) =>
                  setState((current) => ({ ...current, description: event.target.value }))
                }
                placeholder="What this template checks"
              />
            </label>
          </div>

          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium">
              Steps
              <span className="ml-2 text-xs font-normal text-muted-foreground">
                run sequentially, fail-fast per host
              </span>
            </h3>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() =>
                setState((current) => ({ ...current, steps: [...current.steps, emptyStep()] }))
              }
            >
              <Plus /> Add step
            </Button>
          </div>

          <ol className="space-y-3">
            {state.steps.map((step, index) => (
              <li key={step.key} className="rounded-lg border bg-card p-3">
                <div className="mb-2 flex items-center gap-2">
                  <Badge variant="secondary" className="shrink-0">
                    {index + 1}
                  </Badge>
                  <input
                    className={cn(inputClass, 'flex-1')}
                    value={step.name}
                    maxLength={120}
                    onChange={(event) => update(index, { name: event.target.value })}
                    placeholder="Step name"
                    aria-label={`Step ${index + 1} name`}
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    aria-label="Move step up"
                    disabled={index === 0}
                    onClick={() => move(index, -1)}
                  >
                    <ArrowUp />
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    aria-label="Move step down"
                    disabled={index === state.steps.length - 1}
                    onClick={() => move(index, 1)}
                  >
                    <ArrowDown />
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    aria-label="Remove step"
                    disabled={state.steps.length === 1}
                    onClick={() =>
                      setState((current) => ({
                        ...current,
                        steps: current.steps.filter((_, at) => at !== index),
                      }))
                    }
                  >
                    <Trash2 />
                  </Button>
                </div>

                <textarea
                  className={cn(inputClass, 'min-h-32 font-mono text-xs leading-relaxed')}
                  value={step.script}
                  onChange={(event) => update(index, { script: event.target.value })}
                  placeholder="#!/usr/bin/env bash"
                  spellCheck={false}
                  aria-label={`Step ${index + 1} script`}
                />

                <div className="mt-2 grid gap-2 sm:grid-cols-2">
                  <label className="space-y-1 text-xs">
                    <span className="text-muted-foreground">Interpreter</span>
                    <select
                      className={inputClass}
                      value={step.interpreter}
                      onChange={(event) => update(index, { interpreter: event.target.value })}
                      aria-label={`Step ${index + 1} interpreter`}
                    >
                      {!INTERPRETERS.includes(step.interpreter as never) && (
                        <option value={step.interpreter}>{step.interpreter}</option>
                      )}
                      {INTERPRETERS.map((name) => (
                        <option key={name} value={name}>
                          {name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="space-y-1 text-xs">
                    <span className="text-muted-foreground">Timeout (seconds)</span>
                    <input
                      type="number"
                      min={1}
                      max={86400}
                      className={inputClass}
                      value={step.timeout_seconds}
                      onChange={(event) =>
                        update(index, { timeout_seconds: Number(event.target.value) })
                      }
                      aria-label={`Step ${index + 1} timeout`}
                    />
                  </label>
                </div>

                {/*
                  Per-step Run-As, distinct from a workflow Run-As block: this one
                  belongs to the template, so it travels with every workflow that
                  uses it. Empty means "run as the SSH login user", which is why
                  the method selector is hidden until a target is named.
                */}
                <div className="mt-2 rounded-md border border-dashed p-2">
                  <div className="grid gap-2 sm:grid-cols-2">
                    <label className="space-y-1 text-xs">
                      <span className="text-muted-foreground">
                        Run as user (optional)
                      </span>
                      <input
                        className={cn(inputClass, 'font-mono')}
                        value={step.run_as_username}
                        maxLength={32}
                        spellCheck={false}
                        onChange={(event) =>
                          update(index, { run_as_username: event.target.value })
                        }
                        placeholder="Leave empty to use the SSH login user"
                        aria-label={`Step ${index + 1} run as user`}
                      />
                    </label>
                    <label className="space-y-1 text-xs">
                      <span className="text-muted-foreground">Elevation method</span>
                      <select
                        className={inputClass}
                        value={step.run_as_method}
                        disabled={!step.run_as_username.trim()}
                        onChange={(event) =>
                          update(index, {
                            run_as_method: event.target.value as RunAsMethod,
                          })
                        }
                        aria-label={`Step ${index + 1} run as method`}
                      >
                        <option value="nopasswd">Passwordless sudo</option>
                        <option value="password">Sudo password</option>
                      </select>
                    </label>
                  </div>
                  <p className="text-muted-foreground mt-1 text-xs">
                    {!step.run_as_username.trim()
                      ? 'The step runs as the account in the SSH credential.'
                      : step.run_as_method === 'password'
                        ? 'Uses the sudo password stored on the workflow credential. It is never written to the transcript.'
                        : 'Requires a sudoers rule for this target; no password is sent.'}
                  </p>
                </div>

                <RuleEditor
                  rule={step.rule}
                  stepNumber={index + 1}
                  onChange={(rule) => update(index, { rule })}
                  disabled={saving}
                />
              </li>
            ))}
          </ol>
        </div>

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            type="button"
            onClick={() => void submit()}
            disabled={saving || Boolean(validation) || !canSave || blocked}
          >
            {saving ? 'Saving…' : isNew ? 'Create template' : 'Save changes'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

const inputClass =
  'border-input bg-background w-full rounded-md border px-2.5 py-1.5 text-sm outline-none focus-visible:ring-[3px] focus-visible:ring-ring disabled:opacity-50'

export function Notice({
  tone,
  children,
}: {
  tone: 'warning' | 'muted' | 'danger'
  children: React.ReactNode
}) {
  return (
    <div
      role="status"
      className={cn(
        'flex items-start gap-2 rounded-md border px-3 py-2 text-sm',
        tone === 'warning' &&
          'border-amber-600 bg-amber-500/10 text-amber-900 dark:text-amber-200',
        tone === 'muted' && 'bg-muted/50 text-muted-foreground',
        tone === 'danger' && 'border-destructive bg-destructive/5 text-destructive',
      )}
    >
      {/* The icon is what distinguishes the tones for a viewer who cannot separate
          the hues; every tone already carries its meaning in words. */}
      {tone === 'warning' && <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0" />}
      {tone === 'danger' && <AlertCircle aria-hidden="true" className="mt-0.5 size-4 shrink-0" />}
      {tone === 'muted' && <Info aria-hidden="true" className="mt-0.5 size-4 shrink-0" />}
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  )
}
