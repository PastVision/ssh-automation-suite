"""Workflow -> execution bridge tests.

``build_execution_specs`` is the seam between authoring and running: it turns a
saved workflow into the flat specs the engine consumes. It is the highest-risk
function in this milestone, because a mistake here is only visible once a real
run is under way on several hosts.

The behaviours asserted here are the ones the product promises:

* Templates are flattened in pinned order, so a multi-step template runs its
  steps sequentially inside the workflow's own position.
* Run-As attribution is by position -- a block governs everything after it until
  the next block, and never anything before it.
* A host that has since been removed from the workflow is skipped when an
  explicit selection is supplied, rather than silently executing anyway.
* Every spec carries the snapshot identity (template id *and* version id), which
  is what lets history point at exactly what ran.
"""

from __future__ import annotations

import pytest

from app.models.workflow import RunAsMethod
from app.services import workflows
from app.services.sessions import Principal
from tests import lab_support as lab
from tests.test_workflows import _body, _host, _make_template, _principal_for


@pytest.fixture()
def alice(migrated_db) -> Principal:
    return _principal_for(lab.ensure_test_owner("alice"), "alice")


@pytest.fixture()
def credential_id(alice: Principal) -> int:
    """A real credential row owned by alice.

    Ownership matters: a workflow may only reference a credential the caller
    owns, so a credential created for some other test account would be refused by
    the same check the API applies.
    """
    return lab.create_lab_credential(_settings(), owner_id=alice.user_id)


def _settings():
    from app.core.config import get_settings

    return get_settings()


def _load(workflow_id: int):
    """Re-read through the service so every relationship is eagerly loaded.

    ``build_execution_specs`` walks the ORM graph. Reading a workflow detached
    from a closed session would lazy-load against it, so the tests load it the
    same way the launch path does.
    """
    from app.db.session import get_session_factory

    session = get_session_factory()()
    try:
        return workflows._load(session, workflow_id)
    finally:
        session.close()


def _created(alice: Principal, credential_id: int, **kwargs) -> int:
    template_ids = kwargs.pop("template_ids", None)
    entries = kwargs.pop("entries", ())
    hosts = kwargs.pop("hosts", (_host(),))
    variables = kwargs.pop("variables", ())
    body = _body(
        template_ids=template_ids,
        entries=entries,
        hosts=hosts,
        variables=variables,
        credential_id=credential_id,
        **kwargs,
    )
    return workflows.create(body, principal=alice).id


# --------------------------------------------------------------------------- #
# Template flattening
# --------------------------------------------------------------------------- #


def test_a_multistep_template_flattens_in_pinned_order(migrated_db, alice, credential_id):
    """Sequential execution on a host is the product's core promise.

    A three-step template must become three specs, in template order, not one
    combined blob and not sorted by anything incidental.
    """
    template_id = _make_template(alice, "setup", ["echo one\n", "echo two\n", "echo three\n"])
    workflow_id = _created(alice, credential_id, template_ids=[template_id])

    steps, hosts, variables = workflows.build_execution_specs(_load(workflow_id))

    assert [step.script for step in steps] == ["echo one\n", "echo two\n", "echo three\n"]
    assert len(hosts) == 1
    assert variables == []


def test_every_spec_carries_the_snapshot_identity(migrated_db, alice, credential_id):
    """History must be able to name the exact version that ran, not just the name."""
    template_id = _make_template(alice, "deploy", ["echo hi\n"])
    workflow_id = _created(alice, credential_id, template_ids=[template_id])

    steps, _, _ = workflows.build_execution_specs(_load(workflow_id))

    step = steps[0]
    assert step.template_id == template_id
    assert step.template_version == 1
    assert step.template_version_id is not None
    assert step.template_name == "deploy"


def test_the_workflow_order_is_preserved_across_templates(migrated_db, alice, credential_id):
    """Flattening must not interleave: all of A, then all of B."""
    a = _make_template(alice, "a", ["echo a1\n", "echo a2\n"])
    b = _make_template(alice, "b", ["echo b1\n"])
    workflow_id = _created(alice, credential_id, template_ids=[a, b])

    steps, _, _ = workflows.build_execution_specs(_load(workflow_id))

    assert [step.script for step in steps] == ["echo a1\n", "echo a2\n", "echo b1\n"]
    assert [step.template_name for step in steps] == ["a", "a", "b"]


# --------------------------------------------------------------------------- #
# Run-As attribution
# --------------------------------------------------------------------------- #


def test_run_as_applies_only_after_the_block(migrated_db, alice, credential_id):
    """The block is a positional delimiter, so earlier steps keep the primary user.

    This is the difference between "sudo for the install step" and "sudo for
    everything", and getting it backwards would run steps as the wrong account.
    """
    first = _make_template(alice, "before", ["echo before\n"])
    second = _make_template(alice, "after", ["echo after\n", "echo after2\n"])
    workflow_id = _created(
        alice,
        credential_id,
        entries=(
            workflows.StepData(template_id=first),
            workflows.RunAsData(target_username="deploy", method=RunAsMethod.NOPASSWD),
            workflows.StepData(template_id=second),
            workflows.StepData(template_id=first),
        ),
    )

    steps, _, _ = workflows.build_execution_specs(_load(workflow_id))

    assert [step.run_as_username for step in steps] == [
        None,
        "deploy",
        "deploy",
        "deploy",
    ]
    assert all(step.run_as_method is RunAsMethod.NOPASSWD for step in steps[1:])


def test_a_second_block_supersedes_the_first(migrated_db, alice, credential_id):
    """Later blocks must win, which is what makes a multi-stage workflow useful."""
    a = _make_template(alice, "a", ["echo a\n"])
    b = _make_template(alice, "b", ["echo b\n"])
    c = _make_template(alice, "c", ["echo c\n"])
    # The password is supplied at save: a PASSWORD block without one is refused,
    # so there is no way to create this workflow and add the secret afterwards.
    workflow_id = _created(
        alice,
        credential_id,
        entries=(
            workflows.RunAsData(target_username="deploy", method=RunAsMethod.NOPASSWD),
            workflows.StepData(template_id=a),
            workflows.RunAsData(
                target_username="opspw",
                method=RunAsMethod.PASSWORD,
                password="LabSudoPassword1",
            ),
            workflows.StepData(template_id=b),
            workflows.StepData(template_id=c),
        ),
    )

    steps, _, _ = workflows.build_execution_specs(_load(workflow_id))

    assert [(s.run_as_username, s.run_as_method) for s in steps] == [
        ("deploy", RunAsMethod.NOPASSWD),
        ("opspw", RunAsMethod.PASSWORD),
        ("opspw", RunAsMethod.PASSWORD),
    ]


def test_the_block_position_is_carried_onto_each_step(migrated_db, alice, credential_id):
    """History groups child step runs under the transition that produced them.

    Without the position on the spec there is no key to join on, so groups would
    have to be guessed from timestamps.
    """
    a = _make_template(alice, "a", ["echo a\n"])
    workflow_id = _created(
        alice,
        credential_id,
        entries=(
            workflows.RunAsData(target_username="deploy", method=RunAsMethod.NOPASSWD),
            workflows.StepData(template_id=a),
        ),
    )

    steps, _, _ = workflows.build_execution_specs(_load(workflow_id))

    assert steps[0].run_as_position is not None
    assert steps[0].run_as_position == 0


# --------------------------------------------------------------------------- #
# Host selection
# --------------------------------------------------------------------------- #


def test_explicit_host_selection_filters_but_never_invents(migrated_db, alice, credential_id):
    """A stale selection must narrow the run, not extend it to unknown hosts."""
    template_id = _make_template(alice, "t", ["echo hi\n"])
    workflow_id = _created(
        alice,
        credential_id,
        template_ids=[template_id],
        hosts=(_host("one.example.com"), _host("two.example.com")),
    )

    steps, hosts, _ = workflows.build_execution_specs(
        _load(workflow_id), hostnames=["two.example.com", "not-in-the-workflow"]
    )

    assert [host.hostname for host in hosts] == ["two.example.com"]
    assert len(steps) == 1


def test_a_selection_matching_nothing_is_an_error(migrated_db, alice, credential_id):
    """Silently running zero hosts would look like success."""
    template_id = _make_template(alice, "t", ["echo hi\n"])
    workflow_id = _created(alice, credential_id, template_ids=[template_id])

    with pytest.raises(workflows.WorkflowInvalid, match="No hosts"):
        workflows.build_execution_specs(
            _load(workflow_id), hostnames=["absent.example.com"]
        )


def test_a_host_port_falls_back_to_the_credential_default(migrated_db, alice, credential_id):
    template_id = _make_template(alice, "t", ["echo hi\n"])
    workflow_id = _created(alice, credential_id, template_ids=[template_id])

    _, hosts, _ = workflows.build_execution_specs(_load(workflow_id))
    assert hosts[0].port > 0
    assert hosts[0].credential_id == credential_id


# --------------------------------------------------------------------------- #
# Variables
# --------------------------------------------------------------------------- #


def test_variables_are_passed_through_with_their_timeouts(migrated_db, alice, credential_id):
    """Discovery is a real SSH command, so its timeout must survive the hop."""
    template_id = _make_template(alice, "t", ["echo $NODE_NAME\n"])
    workflow_id = _created(
        alice,
        credential_id,
        template_ids=[template_id],
        variables=(
            workflows.VariableData(
                name="NODE_NAME",
                discovery_command="hostname -s",
                description="short hostname",
                timeout_seconds=12,
            ),
        ),
    )

    _, _, variables = workflows.build_execution_specs(_load(workflow_id))

    assert len(variables) == 1
    assert variables[0].name == "NODE_NAME"
    assert variables[0].discovery_command == "hostname -s"
    assert variables[0].timeout_seconds == 12


# --------------------------------------------------------------------------- #
# Refusals: a workflow that cannot run must not start
# --------------------------------------------------------------------------- #


def test_a_workflow_without_a_credential_is_refused(migrated_db, alice, credential_id):
    template_id = _make_template(alice, "t", ["echo hi\n"])
    workflow_id = _created(alice, credential_id, template_ids=[template_id])

    db = workflows.get_session_factory()()
    try:
        from app.models.workflow import Workflow

        workflow = db.get(Workflow, workflow_id)
        workflow.credential_id = None
        db.commit()
    finally:
        db.close()

    with pytest.raises(workflows.WorkflowInvalid, match="no SSH credential"):
        workflows.build_execution_specs(_load(workflow_id))


def test_a_credential_with_no_key_path_is_refused(migrated_db, alice, credential_id):
    """The key path is the only record that a key exists; without it there is
    nothing to connect with, so launch must refuse before opening any host."""
    template_id = _make_template(alice, "t", ["echo hi\n"])
    workflow_id = _created(alice, credential_id, template_ids=[template_id])

    from app.db.session import get_session_factory
    from app.models.credential import SshCredential

    db = get_session_factory()()
    try:
        credential = db.get(SshCredential, credential_id)
        credential.private_key_path = None
        db.commit()
    finally:
        db.close()

    with pytest.raises(workflows.WorkflowInvalid, match="private key"):
        workflows.build_execution_specs(_load(workflow_id))


def test_a_workflow_with_no_steps_is_refused(migrated_db, alice, credential_id):
    """Schema-level emptiness is already blocked, so this is the pinned-version case.

    A step whose version lost its rows would otherwise produce an execution that
    starts and immediately finishes, which reads as success.
    """
    template_id = _make_template(alice, "t", ["echo hi\n"])
    workflow_id = _created(alice, credential_id, template_ids=[template_id])

    from app.db.session import get_session_factory
    from app.models.template import TemplateStep

    db = get_session_factory()()
    try:
        for row in db.query(TemplateStep).all():
            db.delete(row)
        db.commit()
    finally:
        db.close()

    with pytest.raises(workflows.WorkflowInvalid, match="no steps"):
        workflows.build_execution_specs(_load(workflow_id))
