"""Append-only audit trail.

The only writer of ``audit_events``. Two protections apply:

* This module offers no update or delete function, and no route exposes one.
* The database rejects ``UPDATE`` and ``DELETE`` on the table with triggers (see
  the Alembic revision), so even a future careless handler cannot rewrite history.

Secrets must never reach here. ``record`` rejects detail mappings containing a
key that looks like a secret, which turns "we accidentally logged a password"
from a silent leak into a loud programming error during development and tests.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.models.audit import AuditEvent
from app.models.audit_actions import AuditAction, AuditOutcome

logger = logging.getLogger(__name__)

# A detail key containing one of these fragments is refused outright. The match is
# on whole underscore-separated words, not substrings: ``passphrase_set`` is a
# boolean flag, while ``passphrase`` and ``key_passphrase_value`` are not.
_FORBIDDEN_KEY_WORDS = frozenset(
    {
        "password",
        "passphrase",
        "secret",
        "secretvalue",
        "token",
        "privatekey",
        "keymaterial",
        "ciphertext",
        "encrypted",
        "pem",
    }
)

# Values are inspected too: even an innocuously named key must not smuggle a
# multi-line PEM block into the trail.
_PEM_MARKERS = ("PRIVATE KEY", "BEGIN OPENSSH")

# Detail values longer than this are almost certainly a mistake (a script body,
# a log excerpt). Truncated rather than refused so auditing never blocks an
# action, but the trail stays useful.
_MAX_DETAIL_CHARS = 2000


class UnsafeAuditDetail(ValueError):
    """Raised when a caller tries to put secret material into the audit trail."""


def _key_words(key: object) -> set[str]:
    """Split a detail key into lower-case words across snake/kebab/camel case."""
    text = str(key)
    for separator in ("-", ".", "/", " "):
        text = text.replace(separator, "_")
    # Split camelCase so ``privateKey`` is caught as ["private", "key"].
    parts: list[str] = []
    for chunk in text.split("_"):
        buffer = ""
        for character in chunk:
            if character.isupper() and buffer:
                parts.append(buffer.lower())
                buffer = character
            else:
                buffer += character
        if buffer:
            parts.append(buffer.lower())
    return {part for part in parts if part}


def _check_detail(detail: Mapping[str, Any] | None) -> str | None:
    if not detail:
        return None

    for key, value in detail.items():
        # A secret is always text. Booleans, numbers, and None cannot carry one, so
        # a status flag such as ``passphrase_set=True`` is legitimately safe while a
        # string value under a suspicious name is not.
        if isinstance(value, str | bytes):
            words = _key_words(key)
            offending = words & _FORBIDDEN_KEY_WORDS
            if offending:
                raise UnsafeAuditDetail(
                    f"audit detail key {key!r} carries a string value and looks like "
                    f"secret material (matched {sorted(offending)}); audit records must "
                    "never contain secrets."
                )
            if any(marker in value for marker in _PEM_MARKERS):
                raise UnsafeAuditDetail(
                    f"audit detail value for {key!r} contains private key material; "
                    "audit records must never contain secrets."
                )

    encoded = json.dumps(detail, default=str, sort_keys=True)
    if len(encoded) > _MAX_DETAIL_CHARS:
        encoded = encoded[:_MAX_DETAIL_CHARS] + "...(truncated)"
    return encoded


def record(
    action: AuditAction | str,
    *,
    actor_id: int | None,
    actor_username: str | None,
    outcome: AuditOutcome | str = AuditOutcome.SUCCESS,
    resource_type: str | None = None,
    resource_id: int | None = None,
    detail: Mapping[str, Any] | None = None,
    remote_addr: str | None = None,
    session_id: int | None = None,
    session: Session | None = None,
) -> None:
    """Append one audit event.

    Accepts an optional ``session`` so a caller can make the audit row part of the
    same transaction as the change it describes. Without one, a short-lived
    session is used and committed immediately, matching the rest of the codebase's
    rule that no transaction spans external I/O.

    Auditing must never break the action it records: a failure here is logged and
    swallowed. That is a deliberate trade-off -- losing one audit row is better
    than a 500 on a successful login -- and it is why the table also carries a
    database-level append-only guard.
    """
    try:
        payload = _check_detail(detail)
    except UnsafeAuditDetail:
        # Re-raise: this is a programming error, and silently dropping the detail
        # would hide the fact that a caller is handling secret material wrongly.
        raise

    event = AuditEvent(
        actor_user_id=actor_id,
        actor_username=actor_username,
        action=str(action),
        outcome=str(outcome),
        resource_type=resource_type,
        resource_id=resource_id,
        detail=payload,
        remote_addr=remote_addr,
        session_id=session_id,
    )

    if session is not None:
        session.add(event)
        return

    own_session = get_session_factory()()
    try:
        own_session.add(event)
        own_session.commit()
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to write audit event %s", action)
        own_session.rollback()
    finally:
        own_session.close()


def list_events(
    *, limit: int = 100, offset: int = 0, actor_id: int | None = None
) -> list[AuditEvent]:
    """Newest-first page of audit events."""
    from sqlalchemy import select

    session = get_session_factory()()
    try:
        statement = select(AuditEvent).order_by(AuditEvent.id.desc())
        if actor_id is not None:
            statement = statement.where(AuditEvent.actor_user_id == actor_id)
        statement = statement.limit(limit).offset(offset)
        return list(session.scalars(statement))
    finally:
        session.close()
