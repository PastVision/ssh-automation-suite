"""ORM models.

Importing this module registers every table on ``Base.metadata``; Alembic's
``env.py`` and the test suite both rely on that side effect.
"""

from app.models.audit import AuditEvent
from app.models.audit_actions import AuditAction, AuditOutcome
from app.models.credential import SshCredential
from app.models.enums import (
    ExecutionStatus,
    ExecutionTrigger,
    HostRunStatus,
    StepRunStatus,
)
from app.models.execution import (
    Execution,
    ExecutionHostRun,
    ExecutionRunAsTransition,
    ExecutionStepRun,
    ExecutionTemplateSnapshot,
    ExecutionVariableDiscovery,
)
from app.models.permission import (
    PermissionLevel,
    ResourceLock,
    ResourcePermission,
    ResourceType,
)
from app.models.profile import UserSshProfile, UserSshSecret
from app.models.schedule import (
    MisfirePolicy,
    PausedReason,
    Schedule,
    ScheduleKind,
    ScheduleRun,
    ScheduleRunOutcome,
    ScheduleState,
    SchedulerLease,
)
from app.models.template import (
    Template,
    TemplateStep,
    TemplateVersion,
    TemplateVisibility,
)
from app.models.user import User, UserSession
from app.models.workflow import (
    RunAsMethod,
    Workflow,
    WorkflowHost,
    WorkflowRunAsBlock,
    WorkflowStep,
    WorkflowVariable,
)

__all__ = [
    "AuditAction",
    "AuditEvent",
    "AuditOutcome",
    "Execution",
    "ExecutionHostRun",
    "ExecutionRunAsTransition",
    "ExecutionStatus",
    "ExecutionStepRun",
    "ExecutionTemplateSnapshot",
    "ExecutionTrigger",
    "ExecutionVariableDiscovery",
    "HostRunStatus",
    "MisfirePolicy",
    "PausedReason",
    "PermissionLevel",
    "RunAsMethod",
    "ResourceLock",
    "ResourcePermission",
    "ResourceType",
    "Schedule",
    "ScheduleKind",
    "ScheduleRun",
    "ScheduleRunOutcome",
    "ScheduleState",
    "SchedulerLease",
    "SshCredential",
    "StepRunStatus",
    "Template",
    "TemplateStep",
    "TemplateVersion",
    "TemplateVisibility",
    "User",
    "UserSession",
    "UserSshProfile",
    "UserSshSecret",
    "Workflow",
    "WorkflowHost",
    "WorkflowRunAsBlock",
    "WorkflowStep",
    "WorkflowVariable",
]
