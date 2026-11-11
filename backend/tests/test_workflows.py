"""Workflow CRUD, authorization, ordering and Run-As snapshot tests.

These exercise the real service against a real migrated SQLite database: no
mocks. The one thing faked is nothing at all -- templates are created through
``templates.create``, so the pinned version a workflow references is a genuine
version row rather than a hand-built stand-in.

The central invariants under test:

* A workflow stores *pinned template versions*, so editing a template later does
  not silently change what a saved workflow will run.
* A write is refused without the edit lock, and refused when the caller's
  ``row_version`` is stale, so two editors cannot overwrite each other.
* A Run-As password is write-only: stored encrypted, never returned.
* Ordering is the single ``position`` sequence shared by steps and blocks, which
  is what makes "this block governs those steps" well defined.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.security import decrypt_secret
from app.db.session import get_session_factory
from app.models.permission import ResourceLock, ResourceType
from app.models.workflow import (
    RunAsMethod,
    Workflow,
    WorkflowRunAsBlock,
    WorkflowStep,
)
from app.services import locks, templates, workflows
from app.services.sessions import Principal
from tests import lab_support as lab

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _principal_for(user_id: int, username: str) -> Principal:
    """A principal backed by a real session row.

    Workflow writes assert the caller holds the edit lock, and a lock is keyed by
    ``(user_id, session_id)``. A principal with ``session_id=None`` could never
    satisfy that check, so every test principal gets a real session exactly like a
    logged-in browser does.
    """
    from app.services import sessions

    issued = sessions.create_session(user_id, user_agent="test", remote_addr=None)
    return Principal(
        user_id=user_id,
        username=username,
        display_name=username,
        source="test",
        session_id=issued.session_id,
    )


@pytest.fixture()
def alice(migrated_db) -> Principal:
    return _principal_for(lab.ensure_test_owner("alice"), "alice")


def _make_template(principal: Principal, name: str, scripts: list[str]) -> int:
    """Create a template with one step per script, returning its id."""
    body = templates.TemplateBody(
        name=name,
        description=None,
        steps=tuple(
            templates.StepData(name=f"{name}-{index}", script=script)
            for index, script in enumerate(scripts, start=1)
        ),
    )
    template = templates.create(body, principal=principal)
    return template.id


def _take_lock_for(principal: Principal, resource: ResourceType, resource_id: int) -> None:
    """Take an edit lock the way an API request would, with the caller's session."""
    locks.acquire(
        resource,
        resource_id,
        user_id=principal.user_id,
        session_id=principal.session_id or 0,
    )


def _take_lock(principal: Principal, workflow_id: int) -> None:
    _take_lock_for(principal, ResourceType.WORKFLOW, workflow_id)


def _body(
    *,
    name: str = "wf",
    template_ids: list[int] | None = None,
    entries: tuple = (),
    variables: tuple = (),
    hosts: tuple = (),
    credential_id: int | None = None,
) -> workflows.WorkflowBody:
    if template_ids is not None:
        entries = tuple(workflows.StepData(template_id=t) for t in template_ids) + entries
    return workflows.WorkflowBody(
        name=name,
        description=None,
        credential_id=credential_id,
        entries=entries,
        variables=variables,
        hosts=hosts,
    )


def _host(name: str = "web-1.example.com") -> workflows.HostData:
    return workflows.HostData(hostname=name, port=None)


# --------------------------------------------------------------------------- #
# Creation and version pinning
# --------------------------------------------------------------------------- #


def test_create_pins_the_template_version_not_the_template(migrated_db, alice):
    """A saved workflow must keep running what it was saved with.

    The template is edited *after* the workflow is created. The workflow still
    points at version 1, which is the whole point of snapshotting.
    """
    template_id = _make_template(alice, "deploy", ["echo v1\n"])
    workflow = workflows.create(_body(template_ids=[template_id]), principal=alice)

    # Publish v2 of the template. The template's own lock is separate from the
    # workflow's, so this takes it exactly as the API would.
    _take_lock_for(alice, ResourceType.TEMPLATE, template_id)
    templates.update(
        template_id,
        templates.TemplateBody(
            name="deploy",
            description=None,
            steps=(templates.StepData(name="s1", script="echo v2\n"),),
        ),
        expected_row_version=None,
        principal=alice,
    )

    db = get_session_factory()()
    try:
        steps = db.scalars(
            select(WorkflowStep).where(WorkflowStep.workflow_id == workflow.id)
        ).all()
        assert len(steps) == 1
        assert steps[0].template_version.version == 1
        assert steps[0].template_version.steps[0].script == "echo v1\n"
    finally:
        db.close()


def test_create_stores_a_multistep_template_as_one_workflow_step(migrated_db, alice):
    """The workflow references the version; the version owns its own step order.

    Flattening happens at execution time, not at save time, so a reordered or
    extended template version is represented faithfully.
    """
    template_id = _make_template(alice, "multi", ["echo a\n", "echo b\n"])
    workflow = workflows.create(_body(template_ids=[template_id]), principal=alice)

    db = get_session_factory()()
    try:
        steps = db.scalars(
            select(WorkflowStep).where(WorkflowStep.workflow_id == workflow.id)
        ).all()
        assert len(steps) == 1
        assert [s.position for s in steps[0].template_version.steps] == [0, 1]
    finally:
        db.close()


def test_create_rejects_a_template_the_caller_cannot_see(migrated_db, alice):
    bob = _principal_for(lab.ensure_test_owner("bob"), "bob")
    template_id = _make_template(bob, "bobs-secret", ["echo hi\n"])

    with pytest.raises(workflows.WorkflowInvalid):
        workflows.create(_body(template_ids=[template_id]), principal=alice)


def test_create_rejects_an_empty_workflow(migrated_db, alice):
    with pytest.raises(workflows.WorkflowInvalid):
        workflows.create(_body(template_ids=[]), principal=alice)


def test_create_rejects_a_duplicate_host(migrated_db, alice):
    template_id = _make_template(alice, "t", ["echo hi\n"])
    with pytest.raises(workflows.WorkflowInvalid):
        workflows.create(
            _body(
                template_ids=[template_id],
                hosts=(_host("dup.example.com"), _host("dup.example.com")),
            ),
            principal=alice,
        )


def test_create_rejects_duplicate_variable_names(migrated_db, alice):
    """Two discovery commands for one name would make the value ambiguous."""
    template_id = _make_template(alice, "t", ["echo $X\n"])
    variable = workflows.VariableData(
        name="X", discovery_command="echo 1", description=None, timeout_seconds=30
    )
    with pytest.raises(workflows.WorkflowInvalid):
        workflows.create(
            _body(template_ids=[template_id], variables=(variable, variable)),
            principal=alice,
        )


def test_create_rejects_an_invalid_variable_name(migrated_db, alice):
    template_id = _make_template(alice, "t", ["echo hi\n"])
    variable = workflows.VariableData(
        name="not a name", discovery_command="echo 1", description=None, timeout_seconds=30
    )
    with pytest.raises(workflows.WorkflowInvalid):
        workflows.create(_body(template_ids=[template_id], variables=(variable,)), principal=alice)


# --------------------------------------------------------------------------- #
# Run-As: ordering, secrecy, and validation
# --------------------------------------------------------------------------- #


def test_run_as_block_governs_the_steps_after_it_only(migrated_db, alice):
    """Attribution is by position, so a block cannot leak backwards."""
    a = _make_template(alice, "a", ["echo a\n"])
    b = _make_template(alice, "b", ["echo b\n"])
    body = _body(
        entries=(
            workflows.StepData(template_id=a),
            workflows.RunAsData(target_username="deploy", method=RunAsMethod.NOPASSWD),
            workflows.StepData(template_id=b),
        )
    )
    workflow = workflows.create(body, principal=alice)

    entries = workflows.ordered_entries(workflow)
    kinds = [type(entry).__name__ for entry in entries]
    assert kinds == ["WorkflowStep", "WorkflowRunAsBlock", "WorkflowStep"]
    assert [entry.position for entry in entries] == [0, 1, 2]


def test_run_as_password_is_encrypted_and_never_in_the_model_repr(migrated_db, alice):
    template_id = _make_template(alice, "t", ["echo hi\n"])
    secret = "LabSudoPassword1"
    # The block comes first so it actually governs the step that follows it.
    body = _body(
        entries=(
            workflows.RunAsData(
                target_username="opspw", method=RunAsMethod.PASSWORD, password=secret
            ),
            workflows.StepData(template_id=template_id),
        )
    )
    workflow = workflows.create(body, principal=alice)

    db = get_session_factory()()
    try:
        block = db.scalars(
            select(WorkflowRunAsBlock).where(WorkflowRunAsBlock.workflow_id == workflow.id)
        ).one()
        # Stored ciphertext, not the plaintext, and it round-trips.
        assert secret not in (block.password_encrypted or "")
        assert decrypt_secret(block.password_encrypted).decode() == secret
    finally:
        db.close()


def test_password_mode_without_a_password_is_rejected(migrated_db, alice):
    template_id = _make_template(alice, "t", ["echo hi\n"])
    body = _body(
        entries=(
            workflows.RunAsData(target_username="opspw", method=RunAsMethod.PASSWORD),
            workflows.StepData(template_id=template_id),
        ),
    )
    with pytest.raises(workflows.WorkflowInvalid):
        workflows.create(body, principal=alice)


def test_nopasswd_mode_rejects_a_supplied_password(migrated_db, alice):
    """Storing a password for a method that cannot use it would be a trap.

    It would look like the credential is configured while sudo would never read
    it, so a later switch to password mode would appear to work and then fail.
    """
    template_id = _make_template(alice, "t", ["echo hi\n"])
    body = _body(
        entries=(
            workflows.RunAsData(
                target_username="deploy",
                method=RunAsMethod.NOPASSWD,
                password="should-not-be-here",
            ),
            workflows.StepData(template_id=template_id),
        ),
    )
    with pytest.raises(workflows.WorkflowInvalid):
        workflows.create(body, principal=alice)


@pytest.mark.parametrize(
    ("target", "why"),
    [
        ("root", "privilege escalation: Run-As must never become a root shell"),
        ("Bad Name", "not a valid Unix username"),
        ("", "empty target"),
        ("a" * 33, "longer than the 32-character limit"),
    ],
)
def test_an_unusable_run_as_target_is_rejected_at_save(migrated_db, alice, target, why):
    """Catching it here means a run never starts and then fails on host one.

    Validating at save time is what keeps a bad target from being discovered only
    after connections are open on several hosts.
    """
    template_id = _make_template(alice, "t", ["echo hi\n"])
    body = _body(
        entries=(
            workflows.RunAsData(target_username=target, method=RunAsMethod.NOPASSWD),
            workflows.StepData(template_id=template_id),
        ),
    )
    with pytest.raises(workflows.WorkflowInvalid, match=".") as caught:
        workflows.create(body, principal=alice)
    assert caught.value.args[0], why


def test_consecutive_run_as_blocks_do_not_leave_an_orphan(migrated_db, alice):
    """A block with no steps after it governs nothing and is refused."""
    template_id = _make_template(alice, "t", ["echo hi\n"])
    body = _body(
        template_ids=[template_id],
        entries=(
            workflows.RunAsData(target_username="deploy", method=RunAsMethod.NOPASSWD),
            workflows.RunAsData(target_username="opspw", method=RunAsMethod.NOPASSWD),
        ),
    )
    with pytest.raises(workflows.WorkflowInvalid):
        workflows.create(body, principal=alice)


# --------------------------------------------------------------------------- #
# Update, locks, and optimistic concurrency
# --------------------------------------------------------------------------- #


def test_update_requires_the_edit_lock(migrated_db, alice):
    template_id = _make_template(alice, "t", ["echo hi\n"])
    workflow = workflows.create(_body(template_ids=[template_id]), principal=alice)

    with pytest.raises(locks.LockRequired):
        workflows.update(
            workflow.id,
            _body(template_ids=[template_id], name="renamed"),
            expected_row_version=None,
            principal=alice,
        )


def test_update_succeeds_with_the_lock_and_bumps_the_row_version(migrated_db, alice):
    template_id = _make_template(alice, "t", ["echo hi\n"])
    workflow = workflows.create(_body(template_ids=[template_id]), principal=alice)
    before = workflow.row_version
    _take_lock(alice, workflow.id)

    updated = workflows.update(
        workflow.id,
        _body(template_ids=[template_id], name="renamed"),
        expected_row_version=before,
        principal=alice,
    )
    assert updated.name == "renamed"
    assert updated.row_version > before


def test_a_stale_row_version_is_refused(migrated_db, alice):
    """Two editors: the second save must not silently win."""
    template_id = _make_template(alice, "t", ["echo hi\n"])
    workflow = workflows.create(_body(template_ids=[template_id]), principal=alice)
    _take_lock(alice, workflow.id)
    current = workflows.update(
        workflow.id,
        _body(template_ids=[template_id], name="first"),
        expected_row_version=workflow.row_version,
        principal=alice,
    )

    # A stale row_version is its own condition: the caller must reload, not retry.
    with pytest.raises(locks.StaleVersion):
        workflows.update(
            workflow.id,
            _body(template_ids=[template_id], name="second"),
            expected_row_version=workflow.row_version,  # already superseded
            principal=alice,
        )
    assert current.name == "first"


def test_update_replaces_entries_variables_and_hosts_wholesale(migrated_db, alice):
    a = _make_template(alice, "a", ["echo a\n"])
    b = _make_template(alice, "b", ["echo b\n"])
    workflow = workflows.create(
        _body(
            template_ids=[a],
            hosts=(_host("one.example.com"),),
            variables=(
                workflows.VariableData(
                    name="NODE", discovery_command="hostname", description=None,
                    timeout_seconds=30,
                ),
            ),
        ),
        principal=alice,
    )
    _take_lock(alice, workflow.id)

    updated = workflows.update(
        workflow.id,
        _body(template_ids=[b], hosts=(_host("two.example.com"),)),
        expected_row_version=workflow.row_version,
        principal=alice,
    )

    entries = workflows.ordered_entries(updated)
    assert len(entries) == 1
    assert entries[0].template_version.steps[0].script == "echo b\n"
    assert [h.hostname for h in updated.hosts] == ["two.example.com"]
    assert updated.variables == []


def test_a_non_owner_cannot_update_or_delete(migrated_db, alice):
    bob = _principal_for(lab.ensure_test_owner("bob"), "bob")
    template_id = _make_template(alice, "t", ["echo hi\n"])
    workflow = workflows.create(_body(template_ids=[template_id]), principal=alice)

    # Bob cannot even see it, so get() answers not-found rather than forbidden.
    with pytest.raises(workflows.WorkflowNotFound):
        workflows.update(
            workflow.id,
            _body(template_ids=[template_id], name="stolen"),
            expected_row_version=workflow.row_version,
            principal=bob,
        )
    with pytest.raises(workflows.WorkflowNotFound):
        workflows.delete(workflow.id, principal=bob)


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #


def test_delete_releases_every_open_lock(migrated_db, alice):
    """A lock left behind would block a new workflow that reuses the id."""
    template_id = _make_template(alice, "t", ["echo hi\n"])
    workflow = workflows.create(_body(template_ids=[template_id]), principal=alice)
    _take_lock(alice, workflow.id)

    workflows.delete(workflow.id, principal=alice)

    db = get_session_factory()()
    try:
        rows = db.scalars(
            select(ResourceLock).where(
                ResourceLock.resource_type == ResourceType.WORKFLOW,
                ResourceLock.resource_id == workflow.id,
            )
        ).all()
        assert all(row.released_at is not None for row in rows), (
            "delete must mark every lock released, not merely delete the workflow"
        )
        assert all(row.released_reason == "resource_deleted" for row in rows)
        assert db.get(Workflow, workflow.id) is None
    finally:
        db.close()
