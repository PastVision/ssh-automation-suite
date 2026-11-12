"""Shell-script templates.

A template is a stable, editable identity: name, description, owner and sharing
setting. Its *body* -- the ordered list of shell steps -- lives on an immutable
``TemplateVersion``.

Every save that actually changes something appends a new version. Nothing ever
updates an existing version: a version is the exact content an execution ran, so
rewriting one would retroactively change history. The current version is the one
with the highest version number, which avoids a circular foreign key between the
two tables.

Steps belong to a version, not to the template, so pinning a version pins the
ordered step list. An execution copies what it needs into its own
``ExecutionTemplateSnapshot`` rows, which is why editing a template while a run
is in flight cannot affect that run.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import enum_column, timestamp_column
from app.models.enums import RunAsMethod
from app.models.step_rule import RuleColumns


class TemplateVisibility(StrEnum):
    """Sharing intent, which governs whether grants may exist.

    ``PRIVATE`` means "nobody but the owner", and switching to it deletes every
    grant (and releases locks) so the column and the grant table cannot disagree.
    ``SHARED`` means "the owner may grant access", and a grant is required.

    ``PUBLIC`` means any *authenticated* user may view the resource. It is not an
    anonymous/internet exposure: every route still requires a session. Public
    grants viewing only -- never editing -- and never execution rights, which stay
    governed by ``Workflow.execution_permission``.
    """

    PRIVATE = "private"
    SHARED = "shared"
    PUBLIC = "public"


class Template(Base):
    __tablename__ = "templates"
    __table_args__ = (
        # A template name is unique only within its owner's namespace.
        UniqueConstraint("owner_id", "name", name="uq_templates_owner_id_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    visibility: Mapped[TemplateVisibility] = mapped_column(
        enum_column(TemplateVisibility, "template_visibility"),
        nullable=False,
        default=TemplateVisibility.PRIVATE,
    )

    # Optimistic concurrency token. Bumped on every accepted edit so a stale
    # client update is rejected instead of overwriting a newer change.
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)

    versions: Mapped[list[TemplateVersion]] = relationship(
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="TemplateVersion.version",
    )

    @property
    def latest_version(self) -> TemplateVersion | None:
        return self.versions[-1] if self.versions else None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Template id={self.id} name={self.name!r}>"


class TemplateVersion(Base):
    """Immutable snapshot of a template body: metadata plus ordered steps."""

    __tablename__ = "template_versions"
    __table_args__ = (
        UniqueConstraint(
            "template_id", "version", name="uq_template_versions_template_id_version"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(
        ForeignKey("templates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    # Metadata is duplicated onto the version so history shows the name and
    # description in force at the time, not today's values.
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Digest over metadata *and* steps. An edit that changes nothing produces no
    # new version, so version numbers only advance for real changes.
    content_checksum: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = timestamp_column()
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    template: Mapped[Template] = relationship(back_populates="versions")
    steps: Mapped[list[TemplateStep]] = relationship(
        back_populates="version",
        cascade="all, delete-orphan",
        order_by="TemplateStep.position",
    )

    @staticmethod
    def compute_checksum(script: str) -> str:
        """SHA-256 of arbitrary script text.

        Also used by the execution layer to checksum a frozen script, so the
        digest format is identical everywhere.
        """
        return hashlib.sha256(script.encode("utf-8")).hexdigest()

    @staticmethod
    def compute_content_checksum(
        *, name: str, description: str | None, steps: list[dict[str, object]]
    ) -> str:
        """Stable digest of everything that defines a version's body.

        Built from a canonical JSON encoding so key order and whitespace cannot
        make an unchanged template look changed.
        """
        payload = {"name": name, "description": description, "steps": steps}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<TemplateVersion template_id={self.template_id} v{self.version}>"


class TemplateStep(Base, RuleColumns):
    """One ordered shell step inside a template version.

    ``position`` is a dense 0-based index, unique per version. Reordering writes
    rows on a new version rather than renumbering existing ones, so a version's
    order can never change after the fact.
    """

    __tablename__ = "template_steps"
    __table_args__ = (
        UniqueConstraint("version_id", "position", name="uq_template_steps_version_id_position"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_id: Mapped[int] = mapped_column(
        ForeignKey("template_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    script: Mapped[str] = mapped_column(Text, nullable=False)
    interpreter: Mapped[str] = mapped_column(String(40), nullable=False, default="bash")
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)

    # Per-step execution identity. NULL means "run as the SSH login user", which
    # is the behaviour of every step written before this column existed. A
    # non-NULL value overrides any workflow-level Run-As block governing this
    # step, so one template can carry steps that run as different users.
    #
    # Only the *non-secret* target and method live here: a template version is an
    # immutable, duplicated content snapshot, so a password must never be part of
    # it. The password is resolved at run time from the workflow's Run-As
    # credential map, keyed by target username.
    run_as_username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    run_as_method: Mapped[RunAsMethod | None] = mapped_column(
        enum_column(RunAsMethod, "run_as_method"), nullable=True
    )

    version: Mapped[TemplateVersion] = relationship(back_populates="steps")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<TemplateStep version_id={self.version_id} pos={self.position}>"
