"""Execution-rule columns shared by step definitions and execution snapshots.

A rule is eight nullable columns rather than a JSON blob or a child table:

* A child table would need its own lifecycle, join and cascade for a 1:1
  relationship that is never queried independently.
* A JSON blob could not be constrained, and would make "every step that fails
  the host" impossible to express in SQL.

``action`` is NOT NULL with a ``continue`` default so a row written before this
milestone reads as the behaviour it actually had. Every condition column stays
NULL for such a row, and ``to_rule()`` reconstructs ``CONTINUE`` from that.

Both ``TemplateStep`` (the definition) and ``ExecutionTemplateSnapshot`` (the
frozen copy) use this mixin, so a snapshot carries the rule in exactly the same
shape it was authored in and history can be explained without joining back to a
template that may since have changed or been deleted.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.enums import (
    ConditionLocation,
    MatchOperator,
    OutputStream,
    StepAction,
)
from app.services.conditions import Condition, StepRule


class RuleColumns:
    """Mixin adding the execution-control columns and their translation."""

    action: Mapped[str] = mapped_column(
        String(32), nullable=False, default=StepAction.CONTINUE.value,
        server_default=StepAction.CONTINUE.value,
    )
    condition_stream: Mapped[str | None] = mapped_column(String(16), nullable=True)
    condition_location: Mapped[str | None] = mapped_column(String(16), nullable=True)
    condition_operator: Mapped[str | None] = mapped_column(String(16), nullable=True)
    condition_expected: Mapped[str | None] = mapped_column(Text, nullable=True)
    condition_case_sensitive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    condition_line_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    condition_anchor: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    def to_rule(self) -> StepRule:
        """Rebuild the rule. Rows predating this feature yield ``CONTINUE``.

        A row whose condition columns are incomplete cannot be represented as a
        rule; that is treated as ``CONTINUE`` rather than as a match, because
        inventing an action from partial data could skip or fail work the author
        never asked to skip or fail.
        """
        try:
            action = StepAction(self.action)
        except ValueError:
            return StepRule()
        if action is StepAction.CONTINUE:
            return StepRule()
        try:
            condition = Condition(
                stream=OutputStream(self.condition_stream or ""),
                location=ConditionLocation(self.condition_location or ""),
                operator=MatchOperator(self.condition_operator or ""),
                expected=self.condition_expected or "",
                case_sensitive=bool(self.condition_case_sensitive),
                line_number=self.condition_line_number,
                anchor=self.condition_anchor,
            )
        except ValueError:
            return StepRule()
        if not condition.expected or (
            condition.location is ConditionLocation.ANCHOR and not condition.anchor
        ):
            return StepRule()
        return StepRule(action=action, condition=condition)

    def set_rule(self, rule: StepRule) -> None:
        """Write a validated rule onto the row."""
        self.action = rule.action.value
        condition = rule.condition
        if condition is None:
            self.condition_stream = None
            self.condition_location = None
            self.condition_operator = None
            self.condition_expected = None
            self.condition_case_sensitive = False
            self.condition_line_number = None
            self.condition_anchor = None
            return
        self.condition_stream = condition.stream.value
        self.condition_location = condition.location.value
        self.condition_operator = condition.operator.value
        self.condition_expected = condition.expected
        self.condition_case_sensitive = condition.case_sensitive
        self.condition_line_number = condition.line_number
        self.condition_anchor = condition.anchor
