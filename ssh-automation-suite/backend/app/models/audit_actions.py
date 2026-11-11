"""Audit action vocabulary.

Kept as plain strings in the database (not a SQL enum) so adding an action never
needs a migration. This module is the single list of valid values so the UI and
tests agree with the service.
"""

from __future__ import annotations

from enum import StrEnum


class AuditAction(StrEnum):
    # Authentication
    LOGIN_SUCCEEDED = "auth.login.succeeded"
    LOGIN_FAILED = "auth.login.failed"
    LOGOUT = "auth.logout"
    SESSION_CREATED = "auth.session.created"
    SESSION_REVOKED = "auth.session.revoked"
    SESSION_EXPIRED = "auth.session.expired"
    CSRF_REJECTED = "auth.csrf.rejected"
    # Authorization
    ACCESS_DENIED = "authz.access_denied"
    # Credentials and profile
    CREDENTIAL_CREATED = "credential.created"
    CREDENTIAL_UPDATED = "credential.updated"
    CREDENTIAL_DELETED = "credential.deleted"
    PROFILE_UPDATED = "profile.updated"
    SECRET_SET = "profile.secret.set"
    SECRET_CLEARED = "profile.secret.cleared"
    # Shared-resource edits
    RESOURCE_CREATED = "resource.created"
    RESOURCE_UPDATED = "resource.updated"
    RESOURCE_DELETED = "resource.deleted"
    # Templates
    TEMPLATE_CREATED = "template.created"
    TEMPLATE_UPDATED = "template.updated"
    TEMPLATE_DELETED = "template.deleted"
    TEMPLATE_DUPLICATED = "template.duplicated"
    TEMPLATE_VERSION_CREATED = "template.version_created"
    TEMPLATE_SHARED = "template.shared"
    TEMPLATE_UNSHARED = "template.unshared"
    TEMPLATE_VISIBILITY_CHANGED = "template.visibility_changed"
    LOCK_ACQUIRED = "lock.acquired"
    LOCK_RENEWED = "lock.renewed"
    LOCK_RELEASED = "lock.released"
    LOCK_EXPIRED = "lock.expired"
    LOCK_STOLEN = "lock.stale_update_rejected"
    # Executions
    EXECUTION_CREATED = "execution.created"
    EXECUTION_COMPLETED = "execution.completed"
    EXECUTION_CANCELLED = "execution.cancelled"
    # Workflows
    WORKFLOW_CREATED = "workflow.created"
    WORKFLOW_UPDATED = "workflow.updated"
    WORKFLOW_DELETED = "workflow.deleted"
    WORKFLOW_RUN_STARTED = "workflow.run_started"
    WORKFLOW_SHARED = "workflow.shared"
    WORKFLOW_UNSHARED = "workflow.unshared"
    # Run-As, recorded per host transition actually attempted rather than per save.
    RUN_AS_USED = "execution.run_as_used"
    RUN_AS_FAILED = "execution.run_as_failed"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"
