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

export interface HealthResponse {
  status: HealthStatus
  app: string
  version: string
  environment: string
  scheduler_enabled: boolean
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
