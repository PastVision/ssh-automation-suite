/**
 * Workflow editor dialog.
 *
 * The workflow body is one flat, ordered `entries` list, so "which Run-As block
 * governs this template?" is answered by array position. Reordering always moves
 * whole entries, never a splice of an inner list, so the block structure cannot
 * be silently broken.
 *
 * The lock is taken on open and released on close, exactly as the template editor
 * does, because a workflow save is refused with `423` without it.
 *
 * A password-mode Run-As block is the one field that cannot round-trip: the
 * backend rewrites every entry on save and no read model returns the password.
 * The block says so in place rather than letting the save fail with a message the
 * user has no way to act on.
 */

import { ArrowDown, ArrowUp, GripVertical, Plus, Trash2 } from 'lucide-react'
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
import { useCredentials } from '@/hooks/use-credentials'
import { useTemplates } from '@/hooks/use-templates'
import { useWorkflowLock } from '@/hooks/use-workflows'
import { ApiError } from '@/lib/api-client'
import type {
  ExecutionPermission,
  RunAsMethod,
  VariableSource,
  WorkflowCreate,
  WorkflowDetail,
  WorkflowEntryInput,
  WorkflowHost,
  WorkflowUpdate,
  WorkflowVariable,
  WorkflowVisibility,
} from '@/lib/types'
import { cn } from '@/lib/utils'

const inputClass =
  'w-full rounded-md border bg-background px-2.5 py-1.5 text-sm outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:opacity-50'

const MAX_HOSTS = 500
const MAX_STEPS = 100

/** Mirrors the backend's Unix-username rule for a Run-As target. */
const RUN_AS_NAME = /^[a-z_][a-z0-9_-]{0,31}$/
/** Mirrors `variables.NAME_PATTERN`. */
const VARIABLE_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/

export type DraftEntry =
  | { key: string; kind: 'template'; template_id: number | '' }
  | {
      key: string
      kind: 'run_as'
      target_username: string
      method: RunAsMethod
      password: string
      /** True when the saved workflow already has a password for this block. */
      had_password: boolean
    }

interface DraftHost {
  key: string
  hostname: string
  port: string
}

interface DraftVariable {
  key: string
  name: string
  source: VariableSource
  discovery_command: string
  /**
   * Always a string, so an empty box sends `""` -- which the backend accepts as
   * a real value for a user-defined variable and never stores as null. A
   * command-backed variable sends `null` here instead (see `toVariables`).
   */
  static_value: string
  description: string
  timeout_seconds: string
}

export interface EditorState {
  name: string
  description: string
  credentialId: string
  visibility: WorkflowVisibility
  executionPermission: ExecutionPermission
  entries: DraftEntry[]
  hosts: DraftHost[]
  variables: DraftVariable[]
}

let keyCounter = 0
function nextKey(prefix: string): string {
  keyCounter += 1
  return `${prefix}-${keyCounter}`
}

function emptyTemplateEntry(): DraftEntry {
  return { key: nextKey('entry'), kind: 'template', template_id: '' }
}

function emptyRunAsEntry(): DraftEntry {
  return {
    key: nextKey('entry'),
    kind: 'run_as',
    target_username: '',
    method: 'nopasswd',
    password: '',
    had_password: false,
  }
}

export function initialEditor(workflow: WorkflowDetail | null): EditorState {
  if (!workflow) {
    return {
      name: '',
      description: '',
      credentialId: '',
      visibility: 'private',
      executionPermission: 'owner_only',
      entries: [emptyTemplateEntry()],
      hosts: [],
      variables: [],
    }
  }
  return {
    name: workflow.name,
    description: workflow.description ?? '',
    credentialId: workflow.credential_id === null ? '' : String(workflow.credential_id),
    visibility: workflow.visibility,
    executionPermission: workflow.execution_permission,
    entries: workflow.entries.map((entry) =>
      entry.kind === 'template'
        ? {
            key: nextKey('entry'),
            kind: 'template' as const,
            template_id: entry.template_id,
          }
        : {
            key: nextKey('entry'),
            kind: 'run_as' as const,
            target_username: entry.target_username,
            method: entry.method,
            password: '',
            had_password: entry.has_password,
          },
    ),
    hosts: workflow.hosts.map((host) => ({
      key: nextKey('host'),
      hostname: host.hostname,
      port: host.port === null ? '' : String(host.port),
    })),
    variables: workflow.variables.map((variable) => ({
      key: nextKey('var'),
      name: variable.name,
      source: variable.source,
      discovery_command: variable.discovery_command,
      static_value: variable.static_value ?? '',
      description: variable.description ?? '',
      timeout_seconds: String(variable.timeout_seconds),
    })),
  }
}

/** Validate the whole draft, returning the first problem found. */
export function validateWorkflow(state: EditorState): string | null {
  if (!state.name.trim()) return 'A workflow needs a name.'
  if (state.name.trim().length > 120) return 'The name may be at most 120 characters.'

  if (state.entries.length > MAX_STEPS) {
    return `A workflow may contain at most ${MAX_STEPS} entries.`
  }
  const templates = state.entries.filter((entry) => entry.kind === 'template')
  if (templates.length === 0) return 'Add at least one template step.'
  for (const [index, entry] of state.entries.entries()) {
    if (entry.kind !== 'template') continue
    if (entry.template_id === '') {
      return `Entry ${index + 1} needs a template.`
    }
  }

  for (const [index, entry] of state.entries.entries()) {
    if (entry.kind !== 'run_as') continue
    if (!RUN_AS_NAME.test(entry.target_username.trim())) {
      return `Run-As block ${index + 1} needs a valid Unix username (lowercase letters, digits, "_" or "-", at most 32 characters).`
    }
    if (entry.target_username.trim() === 'root') {
      return `Run-As block ${index + 1} cannot target root.`
    }
    // A block governs the templates after it, so a trailing block does nothing.
    const following = state.entries.slice(index + 1)
    if (!following.some((later) => later.kind === 'template')) {
      return `The Run-As block for "${entry.target_username.trim()}" has no template steps after it, so it would do nothing.`
    }
    if (following[0]?.kind === 'run_as') {
      return 'Two Run-As blocks cannot be adjacent. Put a template step between them.'
    }
    if (entry.method === 'password' && !entry.password) {
      return `Run-As block ${index + 1} uses password mode, so its sudo password must be entered. It is never stored back into the form.`
    }
  }

  if (state.hosts.length > MAX_HOSTS) {
    return `A workflow may target at most ${MAX_HOSTS} hosts.`
  }
  const seenHosts = new Set<string>()
  for (const [index, host] of state.hosts.entries()) {
    const hostname = host.hostname.trim()
    if (!hostname) return `Host ${index + 1} needs a hostname.`
    if (hostname.length > 255) return `Host ${index + 1} has a hostname that is too long.`
    const key = hostname.toLowerCase()
    if (seenHosts.has(key)) return `Host "${hostname}" is listed more than once.`
    seenHosts.add(key)
    if (host.port.trim()) {
      const port = Number(host.port)
      if (!Number.isInteger(port) || port < 1 || port > 65535) {
        return `Host "${hostname}" has a port outside 1-65535.`
      }
    }
  }

  const seenVariables = new Set<string>()
  for (const variable of state.variables) {
    const name = variable.name.trim()
    if (!VARIABLE_NAME.test(name)) {
      // Named after the row's position and its current text, so the operator can
      // find the offending field without counting: a fresh, untouched row must
      // not read as an error about "" being malformed.
      return `Variable ${describeVariable(variable)} has an invalid name: use letters, digits and underscore, starting with a letter or underscore, at most 64 characters.`
    }
    if (seenVariables.has(name)) return `Variable "${name}" is defined more than once.`
    seenVariables.add(name)

    // Only the field the chosen source actually uses is validated. A stale
    // discovery command on a user-defined variable is irrelevant, and requiring
    // one there was what made switching source impossible.
    if (variable.source === 'command_output' && !variable.discovery_command.trim()) {
      return `Variable "${name}" discovers its value from command output, so it needs a discovery command.`
    }

    const timeout = Number(variable.timeout_seconds)
    if (!Number.isInteger(timeout) || timeout < 1 || timeout > 3600) {
      return `Variable "${name}" needs a timeout of 1-3600 seconds.`
    }
  }

  return null
}

/** How to name a variable in a message when its own name may be unusable. */
function describeVariable(variable: DraftVariable): string {
  const name = variable.name.trim()
  if (name) return `"${name}"`
  return variable.key.replace('var-', '#')
}

function toEntries(state: EditorState): WorkflowEntryInput[] {
  return state.entries.map((entry) =>
    entry.kind === 'template'
      ? { kind: 'template' as const, template_id: Number(entry.template_id) }
      : {
          kind: 'run_as' as const,
          target_username: entry.target_username.trim(),
          method: entry.method,
          password: entry.method === 'password' ? entry.password : null,
        },
  )
}

function toHosts(state: EditorState): WorkflowHost[] {
  return state.hosts.map((host) => ({
    hostname: host.hostname.trim(),
    port: host.port.trim() ? Number(host.port) : null,
  }))
}

function toVariables(state: EditorState): WorkflowVariable[] {
  return state.variables.map((variable) => ({
    name: variable.name.trim(),
    source: variable.source,
    // Each source sends only its own field as meaningful: the backend stores
    // both columns, and a leftover value in the unused one would be confusing
    // in history even though it is never read.
    discovery_command:
      variable.source === 'command_output' ? variable.discovery_command : '',
    static_value: variable.source === 'user_defined' ? variable.static_value : null,
    description: variable.description.trim() ? variable.description.trim() : null,
    timeout_seconds: Number(variable.timeout_seconds),
  }))
}

export function toCreatePayload(state: EditorState): WorkflowCreate {
  return {
    name: state.name.trim(),
    description: state.description.trim() ? state.description.trim() : null,
    credential_id: state.credentialId ? Number(state.credentialId) : null,
    entries: toEntries(state),
    variables: toVariables(state),
    hosts: toHosts(state),
    visibility: state.visibility,
    execution_permission: state.executionPermission,
  }
}

export function toUpdatePayload(state: EditorState, rowVersion: number): WorkflowUpdate {
  return { ...toCreatePayload(state), row_version: rowVersion }
}

interface WorkflowEditorProps {
  open: boolean
  /** `null` creates a new workflow. */
  workflow: WorkflowDetail | null
  onOpenChange: (open: boolean) => void
  onCreate: (payload: WorkflowCreate) => Promise<unknown>
  onSave: (payload: WorkflowUpdate, rowVersion: number) => Promise<unknown>
  onReload: () => Promise<unknown>
  saving: boolean
  error: unknown
}

export function WorkflowEditor({
  open,
  workflow,
  onOpenChange,
  onCreate,
  onSave,
  onReload,
  saving,
  error,
}: WorkflowEditorProps) {
  const isNew = workflow === null
  const [state, setState] = useState<EditorState>(() => initialEditor(null))
  const [rowVersion, setRowVersion] = useState(1)
  const [hostNotice, setHostNotice] = useState<string | null>(null)

  const lock = useWorkflowLock(workflow?.id ?? null, open && !isNew)
  const credentials = useCredentials()
  const templates = useTemplates('', false)

  useEffect(() => {
    if (!open) return
    setState(initialEditor(workflow))
    setRowVersion(workflow?.row_version ?? 1)
  }, [open, workflow])

  const blocked = !isNew && lock.blockedBy !== null
  const canSave = (isNew || lock.held !== null) && !saving

  const validation = useMemo(() => validateWorkflow(state), [state])
  // A save-time rejection that names one entry, so the error can be shown on that
  // row instead of only in the banner. It stays tied to the failed submission until
  // the next one, which is what "this is why the last save was refused" means.
  const entryError = useMemo(() => entryErrorOf(error), [error])

  function patch(changes: Partial<EditorState>) {
    setState((current) => ({ ...current, ...changes }))
  }

  function moveEntry(index: number, delta: number) {
    setState((current) => {
      const target = index + delta
      if (target < 0 || target >= current.entries.length) return current
      const entries = [...current.entries]
      const [moved] = entries.splice(index, 1)
      entries.splice(target, 0, moved)
      return { ...current, entries }
    })
  }

  function updateEntry(index: number, changes: Partial<DraftEntry>) {
    setState((current) => ({
      ...current,
      entries: current.entries.map((entry, at) =>
        at === index ? ({ ...entry, ...changes } as DraftEntry) : entry,
      ),
    }))
  }

  function removeEntry(index: number) {
    setState((current) => ({
      ...current,
      entries: current.entries.filter((_, at) => at !== index),
    }))
  }

  function updateHost(index: number, changes: Partial<DraftHost>) {
    setState((current) => ({
      ...current,
      hosts: current.hosts.map((host, at) =>
        at === index ? { ...host, ...changes } : host,
      ),
    }))
  }

  function removeHost(index: number) {
    setState((current) => ({
      ...current,
      hosts: current.hosts.filter((_, at) => at !== index),
    }))
  }

  function updateVariable(index: number, changes: Partial<DraftVariable>) {
    setState((current) => ({
      ...current,
      variables: current.variables.map((variable, at) =>
        at === index ? { ...variable, ...changes } : variable,
      ),
    }))
  }

  /**
   * Split a pasted block into one row per line, accepting `host`, `host:port`
   * and `user@host:port`. The user part is ignored because the credential owns
   * the SSH username.
   *
   * A hostname may appear only once regardless of port, because the backend
   * deduplicates on the hostname alone. Skipped lines are reported rather than
   * dropped silently.
   */
  function addHostsFromText(text: string) {
    const seen = new Set(state.hosts.map((host) => host.hostname.trim().toLowerCase()))
    const parsed: DraftHost[] = []
    let skipped = 0
    for (const raw of text.split(/[\r\n,]+/)) {
      const line = raw.trim()
      if (!line || line.startsWith('#')) continue
      const withoutUser = line.includes('@') ? line.slice(line.indexOf('@') + 1) : line
      let hostname = withoutUser
      let port = ''
      const match = /^(.*):(\d{1,5})$/.exec(withoutUser)
      if (match) {
        hostname = match[1]
        port = match[2]
      }
      if (!hostname) continue
      if (seen.has(hostname.toLowerCase())) {
        skipped += 1
        continue
      }
      seen.add(hostname.toLowerCase())
      parsed.push({ key: nextKey('host'), hostname, port })
    }
    if (parsed.length > 0) {
      setState((current) => ({ ...current, hosts: [...current.hosts, ...parsed] }))
    }
    setHostNotice(
      skipped > 0
        ? `Added ${parsed.length}. Skipped ${skipped} already-listed host${
            skipped === 1 ? '' : 's'
          } — a hostname may appear once per workflow, whatever its port.`
        : parsed.length > 0
          ? `Added ${parsed.length} host${parsed.length === 1 ? '' : 's'}.`
          : 'No new hostnames found.',
    )
  }

  async function handleSubmit() {
    if (validation) return
    if (isNew) {
      await onCreate(toCreatePayload(state))
    } else {
      await onSave(toUpdatePayload(state, rowVersion), rowVersion)
    }
  }

  const templateOptions = templates.data?.templates ?? []
  const credentialOptions = credentials.data?.items ?? []

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-4xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{isNew ? 'New workflow' : `Edit ${workflow.name}`}</DialogTitle>
          <DialogDescription>
            Entries run in order on every host. A Run-As block applies to the template
            steps after it until the next block.
          </DialogDescription>
        </DialogHeader>

        {blocked && (
          <Notice tone="warning">
            {lock.blockedBy?.display_name ?? lock.blockedBy?.username} is editing this
            workflow. You can read it, but saving is disabled until they finish.
          </Notice>
        )}

        <div className="space-y-5">
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="space-y-1 text-sm">
              <span className="font-medium">Name</span>
              <input
                className={inputClass}
                value={state.name}
                maxLength={120}
                onChange={(event) => patch({ name: event.target.value })}
                placeholder="Nightly patching"
                aria-label="Workflow name"
              />
            </label>
            <label className="space-y-1 text-sm">
              <span className="font-medium">Credential</span>
              <select
                className={inputClass}
                value={state.credentialId}
                onChange={(event) => patch({ credentialId: event.target.value })}
                aria-label="Workflow credential"
              >
                <option value="">No credential selected</option>
                {credentialOptions.map((credential) => (
                  <option key={credential.id} value={String(credential.id)}>
                    {credential.name} ({credential.username})
                  </option>
                ))}
              </select>
            </label>
          </div>

          <label className="block space-y-1 text-sm">
            <span className="font-medium">Description</span>
            <input
              className={inputClass}
              value={state.description}
              maxLength={2000}
              onChange={(event) => patch({ description: event.target.value })}
              placeholder="What this workflow does"
              aria-label="Workflow description"
            />
          </label>

          {/*
            Visibility and the run permission are separate fields because they
            answer different questions: who can see the definition, and who may
            execute it. The backend enforces both, so the copy states exactly what
            each choice grants rather than implying one implies the other.
          */}
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="space-y-1 text-sm">
              <span className="font-medium">Visibility</span>
              <select
                className={inputClass}
                value={state.visibility}
                onChange={(event) =>
                  patch({ visibility: event.target.value as WorkflowVisibility })
                }
                aria-label="Workflow visibility"
              >
                <option value="private">Private — only me</option>
                <option value="public">Public — every signed-in user</option>
              </select>
              <span className="text-muted-foreground block text-xs">
                {state.visibility === 'private'
                  ? 'Only you can see this workflow and its runs.'
                  : 'Anyone signed in can see the definition. Running it still needs the permission below.'}
              </span>
            </label>
            <label className="space-y-1 text-sm">
              <span className="font-medium">Who may run it</span>
              <select
                className={inputClass}
                value={state.executionPermission}
                onChange={(event) =>
                  patch({
                    executionPermission: event.target.value as ExecutionPermission,
                  })
                }
                aria-label="Workflow execution permission"
              >
                <option value="owner_only">Only the owner</option>
                <option value="selected">Selected users</option>
                <option value="all_authenticated">Any signed-in user</option>
              </select>
              <span className="text-muted-foreground block text-xs">
                {state.executionPermission === 'owner_only'
                  ? 'Shared viewers can read the workflow but cannot start a run.'
                  : state.executionPermission === 'selected'
                    ? 'Grants are managed by the workflow owner; viewers without a grant see a disabled Run button.'
                    : 'Everyone who can see this workflow can start a run with its credential.'}
              </span>
            </label>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-medium">
              Entries
              <span className="text-muted-foreground ml-2 text-xs font-normal">
                templates and Run-As blocks, in order
              </span>
            </h3>
            <div className="flex flex-wrap gap-1.5">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => patch({ entries: [...state.entries, emptyTemplateEntry()] })}
              >
                <Plus /> Template step
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => patch({ entries: [...state.entries, emptyRunAsEntry()] })}
              >
                <Plus /> Run-As block
              </Button>
            </div>
          </div>

          <ol className="space-y-2">
            {state.entries.map((entry, index) => (
              <li
                key={entry.key}
                className={cn(
                  'bg-card rounded-lg border p-3',
                  entry.kind === 'run_as' && 'border-l-4 border-l-amber-500/60',
                )}
              >
                <div className="flex items-center gap-2">
                  <Badge variant="secondary" className="shrink-0">
                    {index + 1}
                  </Badge>
                  <span className="text-muted-foreground inline-flex items-center gap-1 text-xs">
                    <GripVertical className="size-3" />
                    {entry.kind === 'template' ? 'template' : 'run as'}
                  </span>
                  <span className="ml-auto flex items-center gap-1">
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      aria-label={`Move entry ${index + 1} up`}
                      disabled={index === 0}
                      onClick={() => moveEntry(index, -1)}
                    >
                      <ArrowUp />
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      aria-label={`Move entry ${index + 1} down`}
                      disabled={index === state.entries.length - 1}
                      onClick={() => moveEntry(index, 1)}
                    >
                      <ArrowDown />
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      aria-label={`Remove entry ${index + 1}`}
                      disabled={state.entries.length === 1}
                      onClick={() => removeEntry(index)}
                    >
                      <Trash2 />
                    </Button>
                  </span>
                </div>

                {entry.kind === 'template' ? (
                  <select
                    className={cn(inputClass, 'mt-2')}
                    value={entry.template_id === '' ? '' : String(entry.template_id)}
                    onChange={(event) =>
                      updateEntry(index, {
                        template_id:
                          event.target.value === '' ? '' : Number(event.target.value),
                      })
                    }
                    aria-label={`Entry ${index + 1} template`}
                  >
                    <option value="">Select a template…</option>
                    {templateOptions.map((template) => (
                      <option key={template.id} value={String(template.id)}>
                        {template.name}
                        {template.current_version === null
                          ? ''
                          : ` (v${template.current_version})`}
                      </option>
                    ))}
                  </select>
                ) : (
                  <div className="mt-2 space-y-2">
                    <div className="grid gap-2 sm:grid-cols-2">
                      <label className="space-y-1 text-xs">
                        <span className="text-muted-foreground">Target username</span>
                        <input
                          className={inputClass}
                          value={entry.target_username}
                          maxLength={32}
                          onChange={(event) =>
                            updateEntry(index, { target_username: event.target.value })
                          }
                          placeholder="deploy"
                          aria-label={`Entry ${index + 1} Run-As target`}
                        />
                      </label>
                      <label className="space-y-1 text-xs">
                        <span className="text-muted-foreground">sudo method</span>
                        <select
                          className={inputClass}
                          value={entry.method}
                          onChange={(event) =>
                            updateEntry(index, {
                              method: event.target.value as RunAsMethod,
                            })
                          }
                          aria-label={`Entry ${index + 1} Run-As method`}
                        >
                          <option value="nopasswd">passwordless (sudoers rule)</option>
                          <option value="password">password</option>
                        </select>
                      </label>
                    </div>

                    {entry.method === 'password' && (
                      <label className="block space-y-1 text-xs">
                        <span className="text-muted-foreground">
                          sudo password for {entry.target_username || 'the target'}
                        </span>
                        <input
                          type="password"
                          className={inputClass}
                          value={entry.password}
                          autoComplete="new-password"
                          onChange={(event) =>
                            updateEntry(index, { password: event.target.value })
                          }
                          placeholder={
                            entry.had_password
                              ? 'A password is stored — re-enter it to save'
                              : 'Required'
                          }
                          aria-label={`Entry ${index + 1} Run-As password`}
                        />
                        {entry.had_password && (
                          <span className="text-muted-foreground block">
                            Saving rewrites this block, so the stored password is not
                            carried over. Re-enter it.
                          </span>
                        )}
                      </label>
                    )}
                  </div>
                )}

                {entryError?.index === index && (
                  <p className="mt-2 text-xs font-medium text-destructive" role="alert">
                    {entryError.message}
                  </p>
                )}
              </li>
            ))}
          </ol>

          <div className="space-y-2">
            <h3 className="text-sm font-medium">
              Hosts
              <span className="text-muted-foreground ml-2 text-xs font-normal">
                one per line; {state.hosts.length}/{MAX_HOSTS}
              </span>
            </h3>
            <HostBulkInput onAdd={addHostsFromText} />
            {hostNotice && (
              <p className="text-muted-foreground text-xs" role="status">
                {hostNotice}
              </p>
            )}
            {state.hosts.length > 0 && (
              <div className="max-h-56 space-y-1.5 overflow-y-auto pr-1">
                {state.hosts.map((host, index) => (
                  <div key={host.key} className="flex items-center gap-2">
                    <input
                      className={cn(inputClass, 'flex-1')}
                      value={host.hostname}
                      onChange={(event) =>
                        updateHost(index, { hostname: event.target.value })
                      }
                      aria-label={`Host ${index + 1} hostname`}
                    />
                    <input
                      className={cn(inputClass, 'w-24')}
                      value={host.port}
                      placeholder="port"
                      inputMode="numeric"
                      onChange={(event) => updateHost(index, { port: event.target.value })}
                      aria-label={`Host ${index + 1} port`}
                    />
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      aria-label={`Remove host ${index + 1}`}
                      onClick={() => removeHost(index)}
                    >
                      <Trash2 />
                    </Button>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-medium">
                Variables
                <span className="text-muted-foreground ml-2 text-xs font-normal">
                  resolved once per host, then rendered as {'{{ name }}'}
                </span>
              </h3>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() =>
                  patch({
                    variables: [
                      ...state.variables,
                      {
                        key: nextKey('var'),
                        name: '',
                        source: 'command_output' as const,
                        discovery_command: '',
                        static_value: '',
                        description: '',
                        timeout_seconds: '30',
                      },
                    ],
                  })
                }
              >
                <Plus /> Variable
              </Button>
            </div>

            {state.variables.length === 0 ? (
              <p className="text-muted-foreground text-xs">
                No variables. Add one to inject per-host values into every step.
              </p>
            ) : (
              <div className="space-y-2">
                {state.variables.map((variable, index) => {
                  const nameProblem =
                    variable.name.trim() !== '' && !VARIABLE_NAME.test(variable.name.trim())
                  return (
                    <div key={variable.key} className="bg-card rounded-lg border p-3">
                      <div className="grid gap-2 sm:grid-cols-[1fr_9rem_5rem_auto]">
                        <label className="space-y-1 text-xs">
                          <span className="text-muted-foreground">Name</span>
                          <input
                            className={cn(inputClass, nameProblem && 'border-destructive')}
                            value={variable.name}
                            maxLength={64}
                            aria-invalid={nameProblem || undefined}
                            onChange={(event) =>
                              updateVariable(index, { name: event.target.value })
                            }
                            placeholder="hostname_fqdn"
                            aria-label={`Variable ${index + 1} name`}
                          />
                          {nameProblem ? (
                            <span className="text-destructive block">
                              Letters, digits and underscore only.
                            </span>
                          ) : null}
                        </label>
                        <label className="space-y-1 text-xs">
                          <span className="text-muted-foreground">Source</span>
                          <select
                            className={inputClass}
                            value={variable.source}
                            onChange={(event) =>
                              updateVariable(index, {
                                source: event.target.value as VariableSource,
                              })
                            }
                            aria-label={`Variable ${index + 1} source`}
                          >
                            <option value="command_output">Command output</option>
                            <option value="user_defined">Fixed value</option>
                          </select>
                        </label>
                        <label className="space-y-1 text-xs">
                          <span className="text-muted-foreground">Timeout</span>
                          <input
                            className={inputClass}
                            value={variable.timeout_seconds}
                            inputMode="numeric"
                            onChange={(event) =>
                              updateVariable(index, {
                                timeout_seconds: event.target.value,
                              })
                            }
                            aria-label={`Variable ${index + 1} timeout`}
                          />
                        </label>
                        <div className="flex items-end">
                          <Button
                            type="button"
                            variant="ghost"
                            size="icon"
                            aria-label={`Remove variable ${index + 1}`}
                            onClick={() =>
                              patch({
                                variables: state.variables.filter(
                                  (_, at) => at !== index,
                                ),
                              })
                            }
                          >
                            <Trash2 />
                          </Button>
                        </div>
                      </div>

                      {/*
                        Only the active source's field is rendered, so a value the
                        backend would ignore is never shown as if it applied. The
                        inactive value is still kept in the draft, so switching
                        back and forth loses nothing.
                      */}
                      {variable.source === 'command_output' ? (
                        <label className="mt-2 block space-y-1 text-xs">
                          <span className="text-muted-foreground">Discovery command</span>
                          <input
                            className={cn(inputClass, 'font-mono')}
                            value={variable.discovery_command}
                            onChange={(event) =>
                              updateVariable(index, {
                                discovery_command: event.target.value,
                              })
                            }
                            placeholder="hostname -f"
                            spellCheck={false}
                            aria-label={`Variable ${index + 1} discovery command`}
                          />
                          <span className="text-muted-foreground block">
                            Run on each host once per execution; its trimmed output
                            becomes the value.
                          </span>
                        </label>
                      ) : (
                        <label className="mt-2 block space-y-1 text-xs">
                          <span className="text-muted-foreground">Value</span>
                          <input
                            className={cn(inputClass, 'font-mono')}
                            value={variable.static_value}
                            onChange={(event) =>
                              updateVariable(index, { static_value: event.target.value })
                            }
                            placeholder="Sent to every host as-is"
                            spellCheck={false}
                            aria-label={`Variable ${index + 1} value`}
                          />
                          <span className="text-muted-foreground block">
                            The same value on every host. An empty value is allowed and
                            stays empty rather than falling back to discovery.
                          </span>
                        </label>
                      )}

                      <label className="mt-2 block space-y-1 text-xs">
                        <span className="text-muted-foreground">Description</span>
                        <input
                          className={inputClass}
                          value={variable.description}
                          onChange={(event) =>
                            updateVariable(index, { description: event.target.value })
                          }
                          placeholder="Fully qualified hostname"
                          aria-label={`Variable ${index + 1} description`}
                        />
                      </label>
                    </div>
                  )
                })}
              </div>
            )}
          </div>

          {validation && <Notice tone="muted">{validation}</Notice>}

          <SubmitError error={error} onReload={onReload} />
        </div>

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            type="button"
            onClick={() => void handleSubmit()}
            disabled={!canSave || validation !== null}
          >
            {saving ? 'Saving…' : isNew ? 'Create workflow' : 'Save changes'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function HostBulkInput({ onAdd }: { onAdd: (text: string) => void }) {
  const [text, setText] = useState('')
  return (
    <div className="space-y-1.5">
      <textarea
        className={cn(inputClass, 'min-h-20 font-mono text-xs')}
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder={'10.0.0.1\n10.0.0.2:2222\n# comments are ignored'}
        spellCheck={false}
        aria-label="Paste hostnames"
      />
      <div className="flex justify-end">
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={!text.trim()}
          onClick={() => {
            onAdd(text)
            setText('')
          }}
        >
          <Plus /> Add hosts
        </Button>
      </div>
    </div>
  )
}

/**
 * The entry a save error blames, when the backend named one.
 *
 * `entry_position` is the entry's index in the submitted body, not a Run-As block
 * number, so it is matched against the entry list directly. Anything without that
 * field (a lock conflict, a stale version, a generic 422) yields `null` and keeps
 * its existing banner-only treatment.
 *
 * `ApiError.detail` is a getter that flattens the structured body to a string, so
 * the raw `body` is read here: the position and message only exist as object fields.
 */
function entryErrorOf(
  error: unknown,
): { index: number; message: string } | null {
  if (!(error instanceof ApiError)) return null
  const body = error.body
  if (!body || typeof body !== 'object' || !('detail' in body)) return null
  const detail = (body as { detail?: unknown }).detail
  if (!detail || typeof detail !== 'object') return null
  const position = (detail as { entry_position?: unknown }).entry_position
  const message = (detail as { message?: unknown }).message
  if (typeof position !== 'number' || typeof message !== 'string') return null
  return { index: position, message }
}

function SubmitError({
  error,
  onReload,
}: {
  error: unknown
  onReload: () => Promise<unknown>
}) {
  if (!error) return null
  const status = (error as { status?: number }).status
  const detail =
    (error as { detail?: string | null }).detail ??
    (error instanceof Error ? error.message : 'Could not save this workflow.')

  if (status === 409) {
    return (
      <Notice tone="warning">
        <span className="flex flex-wrap items-center justify-between gap-2">
          <span>{detail}</span>
          <Button type="button" size="sm" variant="outline" onClick={() => void onReload()}>
            Reload
          </Button>
        </span>
      </Notice>
    )
  }
  if (status === 423) {
    return <Notice tone="warning">{detail}</Notice>
  }
  return <Notice tone="danger">{detail}</Notice>
}

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
        'rounded-md border px-3 py-2 text-sm',
        tone === 'warning' &&
          'border-amber-500/40 bg-amber-500/10 text-amber-900 dark:text-amber-200',
        tone === 'muted' && 'bg-muted/50 text-muted-foreground',
        tone === 'danger' && 'border-destructive/40 bg-destructive/10 text-destructive',
      )}
    >
      {children}
    </div>
  )
}
