/**
 * Types mirroring `backend/app/schemas`. Hand-maintained for now; there is only
 * one consumer and the surface is small enough that codegen would be overhead.
 */

export type HealthStatus = 'ok' | 'degraded'

export interface HealthDatabase {
  ok: boolean
  url_scheme: string
  error: string | null
}

export interface HealthConcurrency {
  limit: number
  in_use: number
  available: number
}

export interface HealthPaths {
  data_dir: string
  log_dir: string
  execution_log_dir: string
  private_key_dir: string
}

export interface HealthScheduler {
  running: boolean
  lease_held: boolean
  generation: number | null
}

export interface HealthResponse {
  status: HealthStatus
  app: string
  version: string
  environment: string
  scheduler_enabled: boolean
  scheduler: HealthScheduler
  database: HealthDatabase
  concurrency: HealthConcurrency
  paths: HealthPaths
}

/* -------------------------------------------------------------------------- */
/* Authentication                                                              */
/* -------------------------------------------------------------------------- */

export type AuthMode = 'local' | 'proxy'

export interface User {
  id: number
  username: string
  display_name: string | null
  is_active: boolean
  last_login_at: string | null
  created_at: string | null
}

export interface SessionInfo {
  id: number
  created_at: string
  last_seen_at: string
  expires_at: string
  revoked_at: string | null
  revoked_reason: string | null
  auth_source: string
  user_agent: string | null
  remote_addr: string | null
}

/** Mirrors `SessionStatus`: the anonymous case has a null user. */
export interface AuthStatus {
  authenticated: boolean
  auth_mode: AuthMode
  user: User | null
  csrf_token: string | null
  session: SessionInfo | null
}

export interface LoginResponse {
  user: User
  auth_mode: AuthMode
  csrf_token: string
  session: SessionInfo | null
}

/* -------------------------------------------------------------------------- */
/* SSH profile                                                                 */
/* -------------------------------------------------------------------------- */

/**
 * Secret *state* only. The backend has no response field able to carry key
 * material, a passphrase, or a sudo password -- these booleans are all we get.
 */
export interface SecretState {
  private_key_configured: boolean
  private_key_fingerprint: string | null
  private_key_type: string | null
  private_key_comment: string | null
  passphrase_configured: boolean
  sudo_password_configured: boolean
  sudo_password_updated_at: string | null
}

export interface SshProfile {
  user_id: number
  ssh_username: string
  default_port: number
  connect_timeout_seconds: number | null
  command_timeout_seconds: number | null
  keepalive_seconds: number | null
  secrets: SecretState
}

export interface ProfileUpdate {
  ssh_username?: string | null
  default_port?: number | null
  connect_timeout_seconds?: number | null
  command_timeout_seconds?: number | null
  keepalive_seconds?: number | null
  clear_timeouts?: boolean
}

/* -------------------------------------------------------------------------- */
/* Edit locks                                                                  */
/* -------------------------------------------------------------------------- */

export interface LockHolder {
  resource_type: string
  resource_id: number
  user_id: number
  username: string
  display_name: string | null
  session_id: number
  acquired_at: string
  renewed_at: string
  expires_at: string
  is_mine: boolean
}

export interface LockState {
  resource_type: string
  resource_id: number
  locked: boolean
  lock: LockHolder | null
  can_edit: boolean
  can_view: boolean
  row_version: number | null
}

/* -------------------------------------------------------------------------- */
/* Audit                                                                       */
/* -------------------------------------------------------------------------- */

export interface AuditEvent {
  id: number
  actor_user_id: number | null
  actor_username: string | null
  action: string
  outcome: 'success' | 'failure' | 'denied'
  resource_type: string | null
  resource_id: number | null
  detail: string | null
  remote_addr: string | null
  session_id: number | null
  created_at: string
}

export interface AuditPage {
  events: AuditEvent[]
  limit: number
  offset: number
}
/* -------------------------------------------------------------------------- */
/* Templates                                                                   */
/* -------------------------------------------------------------------------- */
/** Mirrors `TemplateVisibility`. There is no "everyone" level by design. */
export type TemplateVisibility = 'private' | 'shared'

/** Mirrors `StepAction`. `continue` is the default and carries no condition. */
export type StepAction = 'continue' | 'fail_if' | 'continue_if' | 'skip_next_if' | 'terminate_if'
/** Mirrors `OutputStream`. `combined` is an approximation, not a merged timeline. */
export type OutputStream = 'stdout' | 'stderr' | 'combined'
export type ConditionLocation = 'anywhere' | 'line_number' | 'anchor'
/** Mirrors `MatchOperator`. There is deliberately no regex operator. */
export type MatchOperator = 'contains' | 'equals'

export interface StepCondition {
  stream: OutputStream
  location: ConditionLocation
  operator: MatchOperator
  expected: string
  case_sensitive: boolean
  line_number: number | null
  anchor: string | null
}
export interface StepConditionInput {
  stream?: OutputStream
  location?: ConditionLocation
  operator?: MatchOperator
  expected: string
  case_sensitive?: boolean
  line_number?: number | null
  anchor?: string | null
}
export interface StepRuleInput {
  action: StepAction
  condition?: StepConditionInput | null
}
export interface StepRule {
  action: StepAction
  condition: StepCondition | null
}

export interface TemplateStep {
  position: number
  name: string
  description: string | null
  script: string
  interpreter: string
  timeout_seconds: number
  rule: StepRule
}
export interface TemplateStepInput {
  name: string
  description?: string | null
  script: string
  interpreter: string
  timeout_seconds: number
  rule?: StepRuleInput
}
export interface TemplateVersionSummary {
  id: number
  version: number
  name: string
  created_at: string
  step_count: number
}
export interface TemplateVersionDetail extends TemplateVersionSummary {
  description: string | null
  content_checksum: string
  steps: TemplateStep[]
}
export interface TemplateShare {
  user_id: number
  username: string
  display_name: string | null
  level: 'view' | 'edit'
  granted_by_id: number | null
  created_at: string | null
}
export interface TemplateSummary {
  id: number
  name: string
  description: string | null
  owner_id: number | null
  owner_label: string | null
  visibility: TemplateVisibility
  is_owner: boolean
  can_edit: boolean
  row_version: number
  current_version: number | null
  step_count: number
  lock: LockHolder | null
  locked_by_other: boolean
  updated_at: string
  created_at: string
}
export interface TemplateDetail extends TemplateSummary {
  steps: TemplateStep[]
  versions: TemplateVersionSummary[]
  shares: TemplateShare[]
}
export interface TemplateListResponse {
  templates: TemplateSummary[]
  total: number
}
export interface TemplateCreate {
  name: string
  description?: string | null
  visibility?: TemplateVisibility
  steps: TemplateStepInput[]
}
export interface TemplateUpdate {
  name: string
  description?: string | null
  steps: TemplateStepInput[]
  /** Optimistic concurrency: the row_version the editor loaded. */
  row_version: number
}
/* -------------------------------------------------------------------------- */
/* SSH credentials                                                             */
/* -------------------------------------------------------------------------- */
/**
 * A credential as the API returns it. There is no private-key field by design:
 * the backend never serialises key material, so this type cannot represent it.
 */
export interface Credential {
  id: number
  name: string
  username: string
  description: string | null
  default_port: number
  private_key_fingerprint: string | null
  private_key_type: string | null
  private_key_comment: string | null
  has_private_key: boolean
  created_at: string
  updated_at: string
}
export interface CredentialListResponse {
  items: Credential[]
}
export interface CredentialCreate {
  name: string
  username: string
  description?: string | null
  default_port: number
  private_key: string
}
/** The 409 detail returned when a credential is still referenced. */
export interface CredentialDeleteBlocked {
  message: string
  workflows: number
  active_executions: number
}
/* -------------------------------------------------------------------------- */
/* Workflows                                                                   */
/* -------------------------------------------------------------------------- */
/** Mirrors `RunAsMethod`: how the target user is reached via sudo. */
export type RunAsMethod = 'nopasswd' | 'password'

/** One entry of a workflow body. Position is array order, not a field. */
export interface WorkflowTemplateEntry {
  kind: 'template'
  position: number
  template_id: number
  template_name: string
  template_version: number
}
/**
 * A Run-As block. `has_password` reports *state* only: the password is
 * write-only, so no response type can carry it.
 */
export interface WorkflowRunAsEntry {
  kind: 'run_as'
  position: number
  target_username: string
  method: RunAsMethod
  has_password: boolean
}
export type WorkflowEntry = WorkflowTemplateEntry | WorkflowRunAsEntry

export interface WorkflowVariable {
  name: string
  discovery_command: string
  description: string | null
  timeout_seconds: number
}
export interface WorkflowHost {
  hostname: string
  port: number | null
}

export interface WorkflowSummary {
  id: number
  name: string
  description: string | null
  owner_id: number
  row_version: number
  can_edit: boolean
  is_owner: boolean
  step_count: number
  run_as_count: number
  variable_count: number
  host_count: number
  created_at: string
  updated_at: string
}
export interface WorkflowDetail extends WorkflowSummary {
  credential_id: number | null
  entries: WorkflowEntry[]
  variables: WorkflowVariable[]
  hosts: WorkflowHost[]
}
export interface WorkflowListResponse {
  items: WorkflowSummary[]
}

export interface WorkflowTemplateStepInput {
  kind: 'template'
  template_id: number
}
export interface WorkflowRunAsInput {
  kind: 'run_as'
  target_username: string
  method: RunAsMethod
  /** Write-only. Omitted when editing a block whose password is already stored. */
  password?: string | null
}
export type WorkflowEntryInput = WorkflowTemplateStepInput | WorkflowRunAsInput

export interface WorkflowCreate {
  name: string
  description?: string | null
  credential_id?: number | null
  entries: WorkflowEntryInput[]
  variables: WorkflowVariable[]
  hosts: WorkflowHost[]
}
export interface WorkflowUpdate extends WorkflowCreate {
  /** Optimistic concurrency: the row_version the editor loaded. */
  row_version: number
}

export interface WorkflowRunResponse {
  execution_id: number
}

/* -------------------------------------------------------------------------- */
/* Executions                                                                  */
/* -------------------------------------------------------------------------- */
export type ExecutionStatus =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
/** Mirrors `ExecutionTrigger`: whether a person or a schedule started the run. */
export type ExecutionTrigger = 'manual' | 'scheduled'
export type HostRunStatus =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'skipped'
  | 'cancelled'
export type StepRunStatus =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'skipped'
  | 'cancelled'
export interface StepRun {
  position: number
  template_name: string
  status: StepRunStatus
  exit_code: number | null
  duration_ms: number | null
  started_at: string | null
  finished_at: string | null
}
export interface HostRun {
  id: number
  hostname: string
  port: number
  credential_name: string | null
  status: HostRunStatus
  failed_step_position: number | null
  error_message: string | null
  started_at: string | null
  finished_at: string | null
  log_path: string | null
  steps: StepRun[]
}
export interface ExecutionSummary {
  id: number
  workflow_id: number | null
  workflow_name: string
  status: ExecutionStatus
  trigger: ExecutionTrigger
  /** Present only on scheduled runs; `null` for manual ones. */
  schedule_id: number | null
  schedule_name: string | null
  /**
   * The logical firing this run answers, in UTC. Distinct from `started_at`,
   * which is when a worker actually picked it up.
   */
  scheduled_for: string | null
  host_count: number
  succeeded_host_count: number
  failed_host_count: number
  created_at: string | null
  started_at: string | null
  finished_at: string | null
}
export interface ExecutionListResponse {
  items: ExecutionSummary[]
  total: number
}
export interface ExecutionDetail extends ExecutionSummary {
  log_path: string | null
  error_message: string | null
  hosts: HostRun[]
}

/** One host run's filesystem transcript. `content` is empty when unavailable. */
export interface HostTranscript {
  execution_id: number
  host_run_id: number
  hostname: string
  available: boolean
  truncated: boolean
  content: string
}

/* -------------------------------------------------------------------------- */
/* Schedules                                                                   */
/* -------------------------------------------------------------------------- */
/**
 * Mirrors `ScheduleKind`. Every kind except `once` is stored as a cron
 * expression: the backend has one timing engine, so `daily`/`weekly`/`monthly`
 * are conveniences the form turns into cron rather than separate server types.
 */
export type ScheduleKind = 'once' | 'daily' | 'weekly' | 'monthly' | 'cron'
/** Mirrors `MisfirePolicy`: what to do with a firing missed while down. */
export type MisfirePolicy = 'skip' | 'run_once'
/** Mirrors `ScheduleState`; `paused`/`disabled` differ in who paused it. */
export type ScheduleState =
  | 'scheduled'
  | 'active'
  | 'paused'
  | 'disabled'
  | 'completed'
  | 'failed'
  | 'missed'
/** Mirrors `PausedReason`: why the scheduler parked a schedule on its own. */
export type PausedReason = 'owner_inactive' | 'invalid_workflow' | 'unauthorized'

export interface Schedule {
  id: number
  owner_id: number
  workflow_id: number | null
  workflow_name: string
  name: string
  schedule_kind: ScheduleKind
  timezone: string
  cron_expression: string | null
  run_at: string | null
  state: ScheduleState
  enabled: boolean
  paused_reason: PausedReason | null
  misfire_policy: MisfirePolicy
  misfire_grace_seconds: number
  last_fired_for: string | null
  next_run_at: string | null
  row_version: number
  created_at: string
  updated_at: string
}
export interface ScheduleListResponse {
  items: Schedule[]
  total: number
}
/** The writable subset. `row_version` is added by the update call only. */
export interface ScheduleInput {
  name: string
  workflow_id: number
  schedule_kind: ScheduleKind
  timezone: string
  cron_expression?: string | null
  run_at?: string | null
  misfire_policy: MisfirePolicy
  misfire_grace_seconds: number
}
export interface ScheduleUpdate extends ScheduleInput {
  /** Optimistic concurrency: the revision the editor loaded. */
  row_version: number
}
/** One previewed firing. `scheduled_for` is the authoritative UTC instant. */
export interface PreviewFiring {
  scheduled_for: string
  local_time: string
}
export interface SchedulePreviewResponse {
  items: PreviewFiring[]
}
