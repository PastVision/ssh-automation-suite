/**
 * Per-step execution rule editor.
 *
 * A rule is optional and defaults to "always continue". The form only reveals the
 * condition fields once an action needs them, and it only shows the one field the
 * chosen location consumes, because the API rejects a condition that carries a
 * value belonging to a different location.
 *
 * There is deliberately no regex operator: matching untrusted remote output with
 * a user-supplied regex is a denial-of-service surface with no product need.
 */
import type {
  ConditionLocation,
  MatchOperator,
  OutputStream,
  StepAction,
  StepRule,
} from '@/lib/types'

const ACTIONS: Array<{ value: StepAction; label: string; hint: string }> = [
  { value: 'continue', label: 'Always continue', hint: 'Ignore the output.' },
  {
    value: 'fail_if',
    label: 'Fail if…',
    hint: 'Stop this host when the output matches. Other hosts carry on.',
  },
  {
    value: 'continue_if',
    label: 'Continue only if…',
    hint: 'Stop this host when the output does NOT match.',
  },
  {
    value: 'skip_next_if',
    label: 'Skip next step if…',
    hint: 'Skip exactly one following step when the output matches.',
  },
  {
    value: 'terminate_if',
    label: 'Terminate if…',
    hint: 'Stop this host early and report it as terminated, not failed.',
  },
]

const STREAMS: Array<{ value: OutputStream; label: string }> = [
  { value: 'stdout', label: 'stdout' },
  { value: 'stderr', label: 'stderr' },
  { value: 'combined', label: 'stdout + stderr' },
]

const LOCATIONS: Array<{ value: ConditionLocation; label: string }> = [
  { value: 'anywhere', label: 'Anywhere in the output' },
  { value: 'line_number', label: 'On a specific line' },
  { value: 'anchor', label: 'After a line containing…' },
]

const OPERATORS: Array<{ value: MatchOperator; label: string }> = [
  { value: 'contains', label: 'contains' },
  { value: 'equals', label: 'equals' },
]

const inputClass =
  'w-full rounded-md border border-input bg-background px-2 py-1 text-xs shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring'

export function defaultCondition(location: ConditionLocation = 'anywhere') {
  return {
    stream: 'stdout' as OutputStream,
    location,
    operator: 'contains' as MatchOperator,
    expected: '',
    // Mirrors the API default: a rule should not change meaning because the
    // client omitted a flag. The editor sends this value explicitly anyway.
    case_sensitive: false,
    line_number: location === 'line_number' ? 1 : null,
    anchor: null,
  }
}

/** A rule the API will accept, or the reason it would not. */
export function validateRule(rule: StepRule): string | null {
  if (rule.action === 'continue') return null
  const condition = rule.condition
  if (condition === null) return 'Choose what to look for.'
  if (!condition.expected.trim()) return 'Enter the text to look for.'
  if (condition.location === 'line_number') {
    if (!condition.line_number || condition.line_number < 1) {
      return 'Line numbers start at 1.'
    }
  }
  if (condition.location === 'anchor' && !condition.anchor?.trim()) {
    return 'Enter the text that identifies the line.'
  }
  return null
}

interface RuleEditorProps {
  rule: StepRule
  onChange: (rule: StepRule) => void
  stepNumber: number
  disabled?: boolean
}

export function RuleEditor({ rule, onChange, stepNumber, disabled }: RuleEditorProps) {
  const active = ACTIONS.find((entry) => entry.value === rule.action) ?? ACTIONS[0]

  function setAction(action: StepAction) {
    if (action === 'continue') {
      onChange({ action, condition: null })
      return
    }
    onChange({ action, condition: rule.condition ?? defaultCondition() })
  }

  function patchCondition(patch: Partial<NonNullable<StepRule['condition']>>) {
    if (rule.condition === null) return
    const next = { ...rule.condition, ...patch }
    // Switching location must clear the other location's field, or the request
    // carries a stale value the API correctly refuses.
    if (patch.location !== undefined && patch.location !== rule.condition.location) {
      next.line_number = patch.location === 'line_number' ? 1 : null
      next.anchor = patch.location === 'anchor' ? '' : null
    }
    onChange({ ...rule, condition: next })
  }

  return (
    <div className="rounded-md border border-dashed border-border/70 bg-muted/20 p-3">
      <label className="block space-y-1 text-xs">
        <span className="text-muted-foreground">Rule for step {stepNumber}</span>
        <select
          className={inputClass}
          value={rule.action}
          onChange={(event) => setAction(event.target.value as StepAction)}
          aria-label={`Step ${stepNumber} rule action`}
          disabled={disabled}
        >
          {ACTIONS.map((entry) => (
            <option key={entry.value} value={entry.value}>
              {entry.label}
            </option>
          ))}
        </select>
      </label>
      <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">{active.hint}</p>

      {rule.condition !== null && (
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          <label className="space-y-1 text-xs">
            <span className="text-muted-foreground">Stream</span>
            <select
              className={inputClass}
              value={rule.condition.stream}
              onChange={(event) =>
                patchCondition({ stream: event.target.value as OutputStream })
              }
              aria-label={`Step ${stepNumber} rule stream`}
              disabled={disabled}
            >
              {STREAMS.map((entry) => (
                <option key={entry.value} value={entry.value}>
                  {entry.label}
                </option>
              ))}
            </select>
          </label>

          <label className="space-y-1 text-xs">
            <span className="text-muted-foreground">Where</span>
            <select
              className={inputClass}
              value={rule.condition.location}
              onChange={(event) =>
                patchCondition({ location: event.target.value as ConditionLocation })
              }
              aria-label={`Step ${stepNumber} rule location`}
              disabled={disabled}
            >
              {LOCATIONS.map((entry) => (
                <option key={entry.value} value={entry.value}>
                  {entry.label}
                </option>
              ))}
            </select>
          </label>

          {rule.condition.location === 'line_number' && (
            <label className="space-y-1 text-xs">
              <span className="text-muted-foreground">Line number</span>
              <input
                type="number"
                min={1}
                className={inputClass}
                value={rule.condition.line_number ?? 1}
                onChange={(event) =>
                  patchCondition({ line_number: Number(event.target.value) })
                }
                aria-label={`Step ${stepNumber} rule line number`}
                disabled={disabled}
              />
            </label>
          )}

          {rule.condition.location === 'anchor' && (
            <label className="space-y-1 text-xs">
              <span className="text-muted-foreground">Line containing</span>
              <input
                type="text"
                className={inputClass}
                value={rule.condition.anchor ?? ''}
                onChange={(event) => patchCondition({ anchor: event.target.value })}
                placeholder="e.g. disk:"
                aria-label={`Step ${stepNumber} rule anchor`}
                disabled={disabled}
              />
            </label>
          )}

          <label className="space-y-1 text-xs">
            <span className="text-muted-foreground">Comparison</span>
            <select
              className={inputClass}
              value={rule.condition.operator}
              onChange={(event) =>
                patchCondition({ operator: event.target.value as MatchOperator })
              }
              aria-label={`Step ${stepNumber} rule operator`}
              disabled={disabled}
            >
              {OPERATORS.map((entry) => (
                <option key={entry.value} value={entry.value}>
                  {entry.label}
                </option>
              ))}
            </select>
          </label>

          <label className="space-y-1 text-xs sm:col-span-2">
            <span className="text-muted-foreground">Text to look for</span>
            <input
              type="text"
              className={inputClass}
              value={rule.condition.expected}
              onChange={(event) => patchCondition({ expected: event.target.value })}
              aria-label={`Step ${stepNumber} rule expected text`}
              disabled={disabled}
            />
          </label>

          <label className="flex items-center gap-2 text-xs sm:col-span-2">
            <input
              type="checkbox"
              checked={rule.condition.case_sensitive}
              onChange={(event) => patchCondition({ case_sensitive: event.target.checked })}
              aria-label={`Step ${stepNumber} rule case sensitive`}
              disabled={disabled}
            />
            <span className="text-muted-foreground">Match case exactly</span>
          </label>
        </div>
      )}
    </div>
  )
}

/** One-line description of a rule for the read-only step list. */
export function describeRule(rule: StepRule | undefined): string | null {
  if (!rule || rule.action === 'continue' || rule.condition === null) return null
  const action = ACTIONS.find((entry) => entry.value === rule.action)?.label ?? rule.action
  const stream = rule.condition.stream === 'combined' ? 'output' : rule.condition.stream
  const where =
    rule.condition.location === 'line_number'
      ? `line ${rule.condition.line_number} of ${stream}`
      : rule.condition.location === 'anchor'
        ? `${stream} after "${rule.condition.anchor}"`
        : stream
  return `${action} ${where} ${rule.condition.operator} "${rule.condition.expected}"`
}
