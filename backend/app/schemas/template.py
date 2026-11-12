"""Template API models.

Two shapes need explaining.

``TemplateSummary`` is what a list needs: identity, sharing state and enough lock
information to render a conflict badge without a second request. ``TemplateDetail``
adds the current body and the version history.

Nothing here can carry a secret. Steps hold shell text, which the caller wrote and
is already authorized to read; there is no private key, passphrase, sudo password,
credential column or filesystem path in any model, and the service never puts one
in a response.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import (
    ConditionLocation,
    MatchOperator,
    OutputStream,
    RunAsMethod,
    StepAction,
)
from app.models.template import TemplateVisibility
from app.schemas.locks import LockRead
from app.services import run_as


class StepConditionInput(BaseModel):
    """One condition, as submitted by the template editor.

    Field ranges here mirror ``services.conditions``: the service is the authority
    (it re-validates), but rejecting early gives the editor a precise 422 instead
    of a generic failure at save time.
    """

    model_config = ConfigDict(extra="forbid")

    stream: OutputStream = OutputStream.STDOUT
    location: ConditionLocation = ConditionLocation.ANYWHERE
    operator: MatchOperator
    expected: str = Field(min_length=1, max_length=500)
    # Must match ``services.conditions.Condition`` and the column default. If the
    # three disagreed, a client that omits the field would get one behaviour from
    # this schema and another from the engine that actually evaluates the rule.
    case_sensitive: bool = False
    line_number: int | None = Field(default=None, ge=1, le=100_000)
    anchor: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _location_fields_match(self) -> StepConditionInput:
        """Reject fields that would be silently ignored for the chosen location."""
        if self.location is ConditionLocation.LINE_NUMBER:
            if self.line_number is None:
                raise ValueError("line_number is required when location is 'line_number'")
            if self.anchor is not None:
                raise ValueError("anchor is not allowed when location is 'line_number'")
        elif self.location is ConditionLocation.ANCHOR:
            if self.anchor is None:
                raise ValueError("anchor is required when location is 'anchor'")
            if self.line_number is not None:
                raise ValueError(
                    "line_number is not allowed when location is 'anchor'"
                )
        else:
            if self.line_number is not None or self.anchor is not None:
                raise ValueError(
                    "line_number and anchor must be omitted when location is 'anywhere'"
                )
        return self


class StepRuleInput(BaseModel):
    """A step's execution control."""

    model_config = ConfigDict(extra="forbid")

    action: StepAction = StepAction.CONTINUE
    condition: StepConditionInput | None = None

    @model_validator(mode="after")
    def _condition_required_when_action_is_conditional(self) -> StepRuleInput:
        if self.action is not StepAction.CONTINUE and self.condition is None:
            raise ValueError(
                f"action '{self.action.value}' requires a condition"
            )
        if self.action is StepAction.CONTINUE and self.condition is not None:
            raise ValueError("action 'continue' must not carry a condition")
        return self


class TemplateStepInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    script: str = Field(min_length=1)
    interpreter: str = Field(default="bash", max_length=40)
    timeout_seconds: int = Field(default=300, ge=1, le=86400)
    rule: StepRuleInput = Field(default_factory=StepRuleInput)
    # Per-step execution identity. Omitted/empty means the SSH login user. The
    # password is deliberately *not* here: a template version is immutable content
    # that gets copied into history, so it must never carry a secret. Password-mode
    # steps resolve their password from the workflow's Run-As credentials.
    run_as_username: str | None = Field(default=None, max_length=32)
    run_as_method: RunAsMethod | None = None

    @field_validator("run_as_username")
    @classmethod
    def _run_as_username_is_a_unix_account(cls, value: str | None) -> str | None:
        """Validate here as well as in the service, for a precise 422."""
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            run_as.validate_target(cleaned)
        except run_as.RunAsError as exc:
            raise ValueError(str(exc)) from exc
        return cleaned

    @model_validator(mode="after")
    def _run_as_method_requires_a_target(self) -> TemplateStepInput:
        if self.run_as_method is not None and not self.run_as_username:
            raise ValueError(
                "run_as_method requires run_as_username; omit both to run as the "
                "SSH login user"
            )
        return self

    @field_validator("interpreter")
    @classmethod
    def _interpreter_is_a_bare_name(cls, value: str) -> str:
        """Only a simple program name: it is placed into a remote command line."""
        cleaned = value.strip()
        if not cleaned or not cleaned.replace("-", "").replace(".", "").isalnum():
            raise ValueError("interpreter must be a single program name, e.g. 'bash'")
        return cleaned

    @field_validator("script")
    @classmethod
    def _script_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("script must not be empty")
        return value


class StepConditionRead(BaseModel):
    """A stored condition, with the derived fields the editor shows read-only."""

    stream: OutputStream
    location: ConditionLocation
    operator: MatchOperator
    expected: str
    case_sensitive: bool
    line_number: int | None = None
    anchor: str | None = None


class StepRuleRead(BaseModel):
    action: StepAction
    condition: StepConditionRead | None = None


class TemplateStepRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    position: int
    name: str
    description: str | None = None
    script: str
    interpreter: str
    timeout_seconds: int
    rule: StepRuleRead = Field(default_factory=lambda: StepRuleRead(action=StepAction.CONTINUE))
    run_as_username: str | None = None
    run_as_method: RunAsMethod | None = None


class TemplateVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    version: int
    name: str
    description: str | None = None
    content_checksum: str
    created_at: datetime
    step_count: int = 0
    steps: list[TemplateStepRead] = Field(default_factory=list)


class TemplateVersionSummary(BaseModel):
    """History entry without the script bodies, for a compact timeline."""

    id: int
    version: int
    name: str
    created_at: datetime
    step_count: int = 0


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    visibility: TemplateVisibility = TemplateVisibility.PRIVATE
    steps: list[TemplateStepInput] = Field(default_factory=list, max_length=100)


class TemplateUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    steps: list[TemplateStepInput] = Field(default_factory=list, max_length=100)
    # Optimistic concurrency: the ``row_version`` the client last saw. Omitted
    # means "don't check", which is only safe because the lock is also required.
    row_version: int | None = Field(default=None, ge=1)


class TemplateDuplicate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)


class TemplateVisibilityUpdate(BaseModel):
    visibility: TemplateVisibility


class ShareGrant(BaseModel):
    user_id: int = Field(ge=1)
    level: str = Field(default="view", pattern="^(view|edit)$")


class ShareRead(BaseModel):
    user_id: int
    username: str
    display_name: str | None = None
    level: str
    granted_by_id: int | None = None
    created_at: datetime | None = None


class TemplateSummary(BaseModel):
    id: int
    name: str
    description: str | None = None
    owner_id: int | None = None
    owner_label: str | None = None
    visibility: TemplateVisibility
    is_owner: bool
    can_edit: bool
    row_version: int
    current_version: int | None = None
    step_count: int = 0
    lock: LockRead | None = None
    locked_by_other: bool = False
    updated_at: datetime
    created_at: datetime


class TemplateDetail(TemplateSummary):
    steps: list[TemplateStepRead] = Field(default_factory=list)
    versions: list[TemplateVersionSummary] = Field(default_factory=list)
    shares: list[ShareRead] = Field(default_factory=list)


class TemplateListResponse(BaseModel):
    templates: list[TemplateSummary]
    total: int


class TemplateDeleteBlocked(BaseModel):
    """Returned as the 409 detail when a workflow still pins this template."""

    message: str
    workflow_steps: int
    execution_snapshots: int
