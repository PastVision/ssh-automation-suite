"""SSH credential lifecycle.

Scope is deliberately narrow: create, list, delete. There is no update, sharing or
administration, because a credential can be replaced by creating a new one and
deleting the old one once nothing references it.

Secrets never leave this module in a readable form. The private key is validated
*before* anything is written (so a bad key cannot leave a half-configured row),
then encrypted by ``key_store``; only the derived fingerprint/algorithm/comment
are recorded for display.

Ownership is enforced on every operation by filtering on ``owner_id``. A
credential belonging to somebody else is indistinguishable from one that does not
exist, so a caller cannot probe for ids.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.models.audit_actions import AuditAction
from app.models.credential import SshCredential
from app.models.enums import ExecutionStatus
from app.models.execution import Execution
from app.models.workflow import Workflow
from app.services import audit, key_store, sessions

logger = logging.getLogger(__name__)


class CredentialNotFound(LookupError):
    """The credential does not exist, or is not owned by the caller."""


class CredentialConflict(RuntimeError):
    """The credential cannot be created or deleted in its current state."""


class CredentialInvalid(ValueError):
    """The supplied credential data cannot form a usable credential."""


class PassphraseNotSupported(CredentialInvalid):
    """A passphrase was supplied, but this build cannot use one at connect time.

    ``SshCredential.passphrase_encrypted`` exists in the schema, but the execution
    runner loads every client key with ``passphrase=None``. Accepting a passphrase
    would therefore store a secret that can never be used and produce a credential
    that fails to connect with a misleading "could not be parsed" error. Rejecting
    it is the only honest option until the runner is wired for passphrases.
    """


def _owned(session: Session, credential_id: int, user_id: int) -> SshCredential:
    credential = session.scalar(
        select(SshCredential).where(
            SshCredential.id == credential_id,
            SshCredential.owner_id == user_id,
        )
    )
    if credential is None:
        raise CredentialNotFound(credential_id)
    return credential


def list_for_user(user_id: int) -> list[SshCredential]:
    session = get_session_factory()()
    try:
        return list(
            session.scalars(
                select(SshCredential)
                .where(SshCredential.owner_id == user_id)
                .order_by(func.lower(SshCredential.name), SshCredential.id)
            ).all()
        )
    finally:
        session.close()


def get(credential_id: int, *, user_id: int) -> SshCredential:
    session = get_session_factory()()
    try:
        return _owned(session, credential_id, user_id)
    finally:
        session.close()


def create(
    *,
    name: str,
    username: str,
    private_key: str,
    passphrase: str | None,
    description: str | None,
    default_port: int,
    principal: sessions.Principal,
    remote_addr: str | None = None,
) -> SshCredential:
    """Validate, store and record a credential.

    Nothing is persisted until the key parses successfully, so an invalid upload
    cannot leave an unusable credential behind.
    """
    cleaned_name = name.strip()
    cleaned_username = username.strip()
    if not cleaned_name:
        raise CredentialInvalid("A credential needs a name.")
    if not cleaned_username:
        raise CredentialInvalid("A credential needs a username.")
    if not 1 <= default_port <= 65535:
        raise CredentialInvalid(f"Port {default_port} is out of range.")

    if passphrase:
        raise PassphraseNotSupported(
            "Passphrase-protected private keys are not supported yet. Supply a key "
            "without a passphrase."
        )

    # Parse locally first. This raises before any row or file is written.
    try:
        key_store.inspect_private_key(private_key, None)
    except key_store.InvalidPrivateKeyError as exc:
        raise CredentialInvalid(
            "That private key could not be read. Supply an unencrypted OpenSSH "
            "private key."
        ) from exc

    settings = get_settings()
    session = get_session_factory()()
    record: SshCredential | None = None
    info: key_store.PrivateKeyInfo | None = None
    try:
        clash = session.scalar(
            select(SshCredential).where(
                SshCredential.owner_id == principal.user_id,
                func.lower(SshCredential.name) == cleaned_name.lower(),
            )
        )
        if clash is not None:
            raise CredentialConflict(
                f"You already have a credential named {cleaned_name!r}."
            )

        record = SshCredential(
            owner_id=principal.user_id,
            name=cleaned_name,
            username=cleaned_username,
            description=description,
            default_port=default_port,
        )
        session.add(record)
        # The key path is derived from the row id, so flush to obtain one.
        session.flush()

        info = key_store.store_private_key(
            record.id, private_key, None, settings=settings
        )
        record.private_key_path = info.path
        record.private_key_fingerprint = info.fingerprint
        record.private_key_type = info.key_type
        record.private_key_comment = info.comment
        session.commit()
        credential_id = record.id
    except IntegrityError as exc:
        session.rollback()
        # A key file is written before the row commits; remove it so a rejected
        # create cannot leave an orphaned ciphertext behind.
        if record is not None:
            try:
                key_store.delete_private_key(record.id, settings)
            except OSError:  # pragma: no cover - best effort cleanup
                logger.info("Could not clean up an orphaned key file")
        raise CredentialConflict(
            f"You already have a credential named {cleaned_name!r}."
        ) from exc
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    audit.record(
        AuditAction.CREDENTIAL_CREATED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        session_id=principal.session_id,
        resource_type="credential",
        resource_id=credential_id,
        remote_addr=remote_addr,
        detail={
            "name": cleaned_name,
            "username": cleaned_username,
            "default_port": default_port,
            # Metadata only: the key text and passphrase are never recorded.
            "key_type": info.key_type,
            "fingerprint": info.fingerprint,
        },
    )
    return get(credential_id, user_id=principal.user_id)


def references(credential_id: int, user_id: int) -> dict[str, int]:
    """Count what still depends on a credential, for the delete guard."""
    session = get_session_factory()()
    try:
        workflows = session.scalar(
            select(func.count())
            .select_from(Workflow)
            .where(Workflow.credential_id == credential_id)
        )
        active = session.scalar(
            select(func.count())
            .select_from(Workflow)
            .join(Execution, Execution.workflow_id == Workflow.id)
            .where(
                Workflow.credential_id == credential_id,
                Execution.status.in_(
                    (ExecutionStatus.PENDING, ExecutionStatus.RUNNING)
                ),
            )
        )
        return {"workflows": int(workflows or 0), "active_executions": int(active or 0)}
    finally:
        session.close()


def delete(
    credential_id: int,
    *,
    principal: sessions.Principal,
    remote_addr: str | None = None,
) -> None:
    """Delete an owned credential, if nothing still depends on it."""
    usage = references(credential_id, principal.user_id)
    session = get_session_factory()()
    try:
        credential = _owned(session, credential_id, principal.user_id)
        # Active executions are reported first: they are the urgent case, and a
        # running execution always implies a workflow reference too, so checking
        # workflows first would make this branch unreachable.
        if usage["active_executions"]:
            raise CredentialConflict(
                "This credential is in use by a running execution. Wait for it to "
                "finish before deleting the credential."
            )
        if usage["workflows"]:
            raise CredentialConflict(
                f"This credential is used by {usage['workflows']} workflow(s). "
                "Point them at another credential first."
            )

        session.delete(credential)
        session.commit()
        deleted_name = credential.name
    except IntegrityError as exc:
        session.rollback()
        raise CredentialConflict(
            "This credential is still referenced and cannot be deleted."
        ) from exc
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    # Remove the ciphertext only after the row is gone, so a failed delete never
    # leaves a row pointing at a missing key.
    try:
        key_store.delete_private_key(credential_id)
    except OSError:  # pragma: no cover - best effort cleanup
        logger.info("Could not remove key file for credential %s", credential_id)

    audit.record(
        AuditAction.CREDENTIAL_DELETED,
        actor_id=principal.user_id,
        actor_username=principal.label,
        session_id=principal.session_id,
        resource_type="credential",
        resource_id=credential_id,
        remote_addr=remote_addr,
        detail={"name": deleted_name},
    )
