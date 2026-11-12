"""Execution authorization, variable sources and sharing persistence.

Three post-release defects are pinned here, each of which the suite was green
through:

* ``execution_permission`` was recorded but never consulted, so a user holding a
  shared VIEW grant could start SSH work against a workflow set to owner-only.
* A user-defined variable's ``static_value`` was dropped on the way from the
  request to the execution spec, so it resolved to empty output.
* ``visibility`` and ``execution_permission`` were accepted by the schema and then
  discarded by the service, so every workflow was silently private.

These tests drive the real HTTP surface and the real services. They deliberately
assert on what did *not* happen (no execution row, no SSH command) as well as on
status codes, because "returns 403" alone would still pass if a connection had
already been opened.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests import lab_support as lab
from tests.conftest import csrf, sign_in

ALICE = ("alice", "alice-password-1")
BOB = ("bob", "bob-password-1")


@pytest.fixture()
def alice(app_client: TestClient, users) -> tuple[TestClient, str]:
    client = TestClient(app_client.app)
    return client, sign_in(client, *ALICE)


@pytest.fixture()
def bob(app_client: TestClient, users) -> tuple[TestClient, str]:
    client = TestClient(app_client.app)
    return client, sign_in(client, *BOB)


@pytest.fixture()
def carol(app_client: TestClient, users) -> tuple[TestClient, str]:
    """A third account with no relationship to anything Alice created."""
    from app.services import sessions

    sessions.create_user("carol", "carol-password-1", display_name="Carol")
    client = TestClient(app_client.app)
    return client, sign_in(client, "carol", "carol-password-1")


def _template(client: TestClient, token: str, name: str = "t") -> int:
    response = client.post(
        "/api/templates",
        json={
            "name": name,
            "description": None,
            "visibility": "private",
            "steps": [
                {
                    "name": "s1",
                    "script": "echo hi\n",
                    "interpreter": "bash",
                    "timeout_seconds": 60,
                }
            ],
        },
        headers=csrf(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _credential(user_id: int) -> int:
    from app.core.config import get_settings

    return lab.create_lab_credential(get_settings(), owner_id=user_id)


def _workflow(
    client: TestClient,
    token: str,
    *,
    template_id: int,
    credential_id: int | None = None,
    visibility: str | None = None,
    execution_permission: str | None = None,
    variables: list[dict] | None = None,
    name: str = "nightly",
) -> dict:
    payload: dict = {
        "name": name,
        "description": None,
        "credential_id": credential_id,
        "entries": [{"kind": "template", "template_id": template_id}],
        "hosts": [{"hostname": "127.0.0.1", "port": lab.lab_port(1)}],
        "variables": variables or [],
    }
    if visibility is not None:
        payload["visibility"] = visibility
    if execution_permission is not None:
        payload["execution_permission"] = execution_permission
    response = client.post("/api/workflows", json=payload, headers=csrf(token))
    assert response.status_code == 201, response.text
    return response.json()


def _share(workflow_id: int, user_id: int, level: str) -> None:
    """Grant ``user_id`` access to a workflow, as the owner would from the UI."""
    from app.models.permission import PermissionLevel, ResourceType
    from app.services import permissions

    permissions.grant(
        ResourceType.WORKFLOW,
        workflow_id,
        user_id=user_id,
        level=PermissionLevel(level),
        granted_by_id=None,
    )


def _execution_count() -> int:
    from sqlalchemy import func, select

    from app.db.session import get_session_factory
    from app.models.execution import Execution

    session = get_session_factory()()
    try:
        return session.scalar(select(func.count(Execution.id))) or 0
    finally:
        session.close()


def _discoveries_for(host_run_id: int) -> list:
    """Discovery rows for one host run, read inside a live session.

    The relationship lazy-loads, so touching it on a detached execution raises
    instead of returning rows.
    """
    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models.execution import ExecutionVariableDiscovery

    session = get_session_factory()()
    try:
        return list(
            session.scalars(
                select(ExecutionVariableDiscovery).where(
                    ExecutionVariableDiscovery.host_run_id == host_run_id
                )
            )
        )
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# D1 -- execution authorization
# --------------------------------------------------------------------------- #


def test_owner_can_execute(app_client, alice, users):
    client, token = alice
    template_id = _template(client, token)
    workflow = _workflow(
        client,
        token,
        template_id=template_id,
        credential_id=_credential(users["alice"]),
        execution_permission="owner_only",
    )

    response = client.post(f"/api/workflows/{workflow['id']}/run", json={}, headers=csrf(token))
    assert response.status_code == 202, response.text
    assert response.json()["execution_id"] > 0


def test_shared_viewer_cannot_execute_owner_only(app_client, alice, bob, users):
    """The escalation this change exists to close."""
    alice_client, alice_token = alice
    bob_client, bob_token = bob
    template_id = _template(alice_client, alice_token)
    workflow = _workflow(
        alice_client,
        alice_token,
        template_id=template_id,
        credential_id=_credential(users["alice"]),
        execution_permission="owner_only",
    )

    # Bob can see it -- a viewer, not an outsider. The 403 below must therefore be
    # an execution refusal, not an accidental 404 from the visibility check.
    _share(workflow["id"], users["bob"], "view")
    assert (
        bob_client.get(f"/api/workflows/{workflow['id']}", headers=csrf(bob_token)).status_code
        == 200
    )

    before = _execution_count()
    response = bob_client.post(
        f"/api/workflows/{workflow['id']}/run", json={}, headers=csrf(bob_token)
    )
    assert response.status_code == 403, response.text
    assert "run" in response.json()["detail"].lower()
    # Nothing was created, so nothing could have opened a connection either.
    assert _execution_count() == before


def test_shared_editor_cannot_execute_owner_only(app_client, alice, bob, users):
    alice_client, alice_token = alice
    bob_client, bob_token = bob
    template_id = _template(alice_client, alice_token)
    workflow = _workflow(
        alice_client,
        alice_token,
        template_id=template_id,
        credential_id=_credential(users["alice"]),
        execution_permission="owner_only",
    )
    _share(workflow["id"], users["bob"], "edit")

    before = _execution_count()
    response = bob_client.post(
        f"/api/workflows/{workflow['id']}/run", json={}, headers=csrf(bob_token)
    )
    assert response.status_code == 403, response.text
    assert _execution_count() == before


def test_selected_user_can_execute(app_client, alice, bob, users):
    alice_client, alice_token = alice
    bob_client, bob_token = bob
    template_id = _template(alice_client, alice_token)
    workflow = _workflow(
        alice_client,
        alice_token,
        template_id=template_id,
        credential_id=_credential(users["alice"]),
        execution_permission="selected",
    )
    _share(workflow["id"], users["bob"], "view")

    response = bob_client.post(
        f"/api/workflows/{workflow['id']}/run", json={}, headers=csrf(bob_token)
    )
    assert response.status_code == 202, response.text


def test_unselected_user_cannot_execute_selected_policy(app_client, alice, bob, carol, users):
    """A third account with no grant at all is refused under ``selected``."""
    alice_client, alice_token = alice
    carol_client, carol_token = carol
    template_id = _template(alice_client, alice_token)
    workflow = _workflow(
        alice_client,
        alice_token,
        template_id=template_id,
        credential_id=_credential(users["alice"]),
        visibility="public",
        execution_permission="selected",
    )

    before = _execution_count()
    response = carol_client.post(
        f"/api/workflows/{workflow['id']}/run", json={}, headers=csrf(carol_token)
    )
    assert response.status_code == 403, response.text
    assert _execution_count() == before


def test_all_authenticated_users_can_execute(app_client, alice, bob, users):
    alice_client, alice_token = alice
    bob_client, bob_token = bob
    template_id = _template(alice_client, alice_token)
    workflow = _workflow(
        alice_client,
        alice_token,
        template_id=template_id,
        credential_id=_credential(users["alice"]),
        visibility="public",
        execution_permission="all_authenticated",
    )

    response = bob_client.post(
        f"/api/workflows/{workflow['id']}/run", json={}, headers=csrf(bob_token)
    )
    assert response.status_code == 202, response.text


def test_refused_launch_never_opens_a_connection(app_client, alice, bob, users, monkeypatch):
    """A denial must not reach the transport, even though hosts and a key exist.

    The workflow targets a real lab port and carries a real key file, so any code
    that got as far as connecting would succeed. Refusing before that point is what
    this asserts.
    """
    from app.services import ssh_transport

    alice_client, alice_token = alice
    bob_client, bob_token = bob
    template_id = _template(alice_client, alice_token)
    workflow = _workflow(
        alice_client,
        alice_token,
        template_id=template_id,
        credential_id=_credential(users["alice"]),
        execution_permission="owner_only",
    )
    _share(workflow["id"], users["bob"], "view")

    opened: list[str] = []

    async def _explode(*args, **kwargs):
        opened.append("connect")
        raise AssertionError("a refused launch must not reach the SSH transport")

    monkeypatch.setattr(ssh_transport, "connect", _explode)

    response = bob_client.post(
        f"/api/workflows/{workflow['id']}/run", json={}, headers=csrf(bob_token)
    )
    assert response.status_code == 403, response.text
    assert opened == []


# --------------------------------------------------------------------------- #
# D2 -- variable sources
# --------------------------------------------------------------------------- #


def test_static_variable_survives_the_round_trip(app_client, alice, users):
    """The value must come back from the API, not just be accepted by it."""
    client, token = alice
    template_id = _template(client, token)
    workflow = _workflow(
        client,
        token,
        template_id=template_id,
        credential_id=_credential(users["alice"]),
        variables=[
            {
                "name": "release",
                "source": "user_defined",
                "static_value": "2026.11",
                "discovery_command": "",
            }
        ],
    )

    read = client.get(f"/api/workflows/{workflow['id']}", headers=csrf(token))
    assert read.status_code == 200, read.text
    (variable,) = read.json()["variables"]
    assert variable["source"] == "user_defined"
    assert variable["static_value"] == "2026.11"
    assert variable["discovery_command"] == ""


def test_static_value_reaches_the_execution_spec(app_client, alice, users):
    """The seam the defect lived in: request -> model -> ``VariableSpec``."""
    from app.services import workflows
    from app.services.sessions import Principal

    client, token = alice
    template_id = _template(client, token)
    workflow = _workflow(
        client,
        token,
        template_id=template_id,
        credential_id=_credential(users["alice"]),
        variables=[
            {
                "name": "release",
                "source": "user_defined",
                "static_value": "2026.11",
            }
        ],
    )

    principal = Principal(
        user_id=users["alice"], username="alice", display_name="Alice", source="test"
    )
    loaded = workflows.get(workflow["id"], principal)
    _steps, _hosts, specs = workflows.build_execution_specs(loaded)
    (spec,) = [item for item in specs if item.name == "release"]
    assert spec.source is workflows.VariableSource.USER_DEFINED
    assert spec.static_value == "2026.11"
    assert spec.discovery_command == ""


def test_empty_static_value_is_valid(app_client, alice, users):
    """An empty string is a value; it must not be mistaken for "no source"."""
    client, token = alice
    template_id = _template(client, token)
    workflow = _workflow(
        client,
        token,
        template_id=template_id,
        credential_id=_credential(users["alice"]),
        variables=[{"name": "flag", "source": "user_defined", "static_value": ""}],
    )
    read = client.get(f"/api/workflows/{workflow['id']}", headers=csrf(token))
    assert read.status_code == 200, read.text
    (variable,) = read.json()["variables"]
    assert variable["source"] == "user_defined"
    assert variable["static_value"] == ""


def test_user_defined_variable_requires_a_value(app_client, alice, users):
    """``None`` is "not supplied" and is still refused; ``""`` is a value."""
    client, token = alice
    template_id = _template(client, token)
    response = client.post(
        "/api/workflows",
        json={
            "name": "bad",
            "description": None,
            "credential_id": _credential(users["alice"]),
            "entries": [{"kind": "template", "template_id": template_id}],
            "hosts": [{"hostname": "127.0.0.1", "port": lab.lab_port(1)}],
            "variables": [{"name": "flag", "source": "user_defined"}],
        },
        headers=csrf(token),
    )
    assert response.status_code == 422, response.text


def test_user_defined_variable_rejects_a_discovery_command(app_client, alice, users):
    """The two sources stay disjoint, so a variable cannot silently change type."""
    client, token = alice
    template_id = _template(client, token)
    response = client.post(
        "/api/workflows",
        json={
            "name": "bad2",
            "description": None,
            "credential_id": _credential(users["alice"]),
            "entries": [{"kind": "template", "template_id": template_id}],
            "hosts": [{"hostname": "127.0.0.1", "port": lab.lab_port(1)}],
            "variables": [
                {
                    "name": "flag",
                    "source": "user_defined",
                    "static_value": "x",
                    "discovery_command": "whoami",
                }
            ],
        },
        headers=csrf(token),
    )
    assert response.status_code == 422, response.text


def test_static_discovery_never_opens_a_command(app_client, alice, users, settings, monkeypatch):
    """Discovery for a static variable resolves without touching the host.

    Driven through the real ``_discover`` coroutine with a seeded host run, so the
    branch that decides "run a command or use the frozen value" is the code under
    test rather than a reimplementation of it.
    """
    import asyncio

    from app.models.enums import StepRunStatus
    from app.services import execution_state, ssh_transport
    from app.services.execution_log import HostTranscript
    from app.services.execution_runner import CancelToken, ExecutionRunner
    from app.services.execution_state import HostSpec, StepSpec, VariableSpec

    _ = (alice, users)  # the HTTP fixtures give us a migrated database
    credential_id = _credential(users["alice"])

    steps = [
        StepSpec(
            template_name="t",
            template_version=1,
            script="echo hi",
            interpreter="bash",
            timeout_seconds=30,
        )
    ]
    hosts = [
        HostSpec(
            hostname="127.0.0.1",
            port=lab.lab_port(1),
            credential_id=credential_id,
            credential_name="lab",
        )
    ]
    specs = [
        VariableSpec(
            name="release",
            discovery_command="",
            timeout_seconds=30,
            source=execution_state.VariableSource.USER_DEFINED,
            static_value="2026.11",
        ),
        VariableSpec(
            name="blank",
            discovery_command="",
            timeout_seconds=30,
            source=execution_state.VariableSource.USER_DEFINED,
            static_value="",
        ),
    ]
    execution_id = execution_state.create_execution(
        steps=steps,
        hosts=hosts,
        max_concurrent_hosts=1,
        created_by_id=users["alice"],
        variables=specs,
        workflow_name="static",
    )
    host_run_id = execution_state.get_execution(execution_id).host_runs[0].id

    executed: list[str] = []

    async def _explode(*args, **kwargs):
        executed.append("command")
        raise AssertionError("a static variable must not run a discovery command")

    monkeypatch.setattr(ssh_transport, "run_command", _explode)

    transcript = HostTranscript(
        execution_id=execution_id,
        host_run_id=host_run_id,
        hostname="127.0.0.1",
        settings=settings,
    )
    runner = ExecutionRunner(settings)
    runner._variable_specs[execution_id] = specs

    with transcript:
        values = asyncio.run(
            runner._discover(execution_id, host_run_id, None, CancelToken(), transcript)
        )
    assert values == {"release": "2026.11", "blank": ""}
    assert executed == []

    discoveries = {row.name: row for row in _discoveries_for(host_run_id)}
    assert discoveries["release"].status is StepRunStatus.SUCCEEDED
    assert discoveries["release"].value_length == len("2026.11")
    assert discoveries["blank"].value_length == 0


# --------------------------------------------------------------------------- #
# D3 -- visibility and execution permission persistence
# --------------------------------------------------------------------------- #


def test_create_public_workflow_is_retrievable_as_public(app_client, alice, users):
    client, token = alice
    template_id = _template(client, token)
    workflow = _workflow(
        client,
        token,
        template_id=template_id,
        credential_id=_credential(users["alice"]),
        visibility="public",
        execution_permission="all_authenticated",
    )
    assert workflow["visibility"] == "public"
    assert workflow["execution_permission"] == "all_authenticated"

    read = client.get(f"/api/workflows/{workflow['id']}", headers=csrf(token))
    assert read.status_code == 200, read.text
    assert read.json()["visibility"] == "public"
    assert read.json()["execution_permission"] == "all_authenticated"


def test_create_private_workflow_is_retrievable_as_private(app_client, alice, users):
    client, token = alice
    template_id = _template(client, token)
    workflow = _workflow(
        client,
        token,
        template_id=template_id,
        credential_id=_credential(users["alice"]),
        visibility="private",
        execution_permission="owner_only",
    )
    read = client.get(f"/api/workflows/{workflow['id']}", headers=csrf(token))
    assert read.status_code == 200, read.text
    assert read.json()["visibility"] == "private"
    assert read.json()["execution_permission"] == "owner_only"


def test_omitted_create_fields_default_to_private_and_owner_only(app_client, alice, users):
    """A client that predates sharing cannot create something shared."""
    client, token = alice
    template_id = _template(client, token)
    workflow = _workflow(
        client,
        token,
        template_id=template_id,
        credential_id=_credential(users["alice"]),
    )
    assert workflow["visibility"] == "private"
    assert workflow["execution_permission"] == "owner_only"


def _update(client: TestClient, token: str, workflow: dict, **changes) -> dict:
    """PUT the workflow back, optionally changing named fields."""
    from tests.test_workflows_api import _lock

    _lock(client, token, workflow["id"])
    payload = {
        "name": workflow["name"],
        "description": workflow["description"],
        "credential_id": workflow["credential_id"],
        "entries": workflow["entries"],
        "hosts": workflow["hosts"],
        "variables": workflow["variables"],
        "row_version": workflow["row_version"],
    }
    payload.update(changes)
    response = client.put(f"/api/workflows/{workflow['id']}", json=payload, headers=csrf(token))
    assert response.status_code == 200, response.text
    return response.json()


def test_update_visibility_keeps_unrelated_fields(app_client, alice, users):
    client, token = alice
    template_id = _template(client, token)
    workflow = _workflow(
        client, token, template_id=template_id, credential_id=_credential(users["alice"])
    )
    updated = _update(client, token, workflow, visibility="public")
    assert updated["visibility"] == "public"
    # Untouched fields must survive the same save.
    assert updated["execution_permission"] == "owner_only"
    assert updated["name"] == workflow["name"]
    assert updated["credential_id"] == workflow["credential_id"]
    assert [entry["kind"] for entry in updated["entries"]] == ["template"]


def test_update_execution_permission_keeps_visibility(app_client, alice, users):
    client, token = alice
    template_id = _template(client, token)
    workflow = _workflow(
        client,
        token,
        template_id=template_id,
        credential_id=_credential(users["alice"]),
        visibility="public",
    )
    updated = _update(client, token, workflow, execution_permission="selected")
    assert updated["execution_permission"] == "selected"
    assert updated["visibility"] == "public", "visibility must not reset"


def test_update_without_sharing_fields_preserves_them(app_client, alice, users):
    """The regression: a save from a client that omits both must not reset them."""
    client, token = alice
    template_id = _template(client, token)
    workflow = _workflow(
        client,
        token,
        template_id=template_id,
        credential_id=_credential(users["alice"]),
        visibility="public",
        execution_permission="all_authenticated",
    )
    updated = _update(client, token, workflow, description="edited")
    assert updated["description"] == "edited"
    assert updated["visibility"] == "public"
    assert updated["execution_permission"] == "all_authenticated"


def test_returning_a_workflow_to_private_revokes_grants(app_client, alice, bob, users):
    """A revoked share must not silently reactivate if it is made public again."""
    from app.models.permission import ResourceType
    from app.services import permissions

    alice_client, alice_token = alice
    bob_client, bob_token = bob
    template_id = _template(alice_client, alice_token)
    workflow = _workflow(
        alice_client,
        alice_token,
        template_id=template_id,
        credential_id=_credential(users["alice"]),
        visibility="public",
    )
    _share(workflow["id"], users["bob"], "view")
    assert (
        bob_client.get(f"/api/workflows/{workflow['id']}", headers=csrf(bob_token)).status_code
        == 200
    )

    _update(alice_client, alice_token, workflow, visibility="private")
    assert permissions.permission_for(ResourceType.WORKFLOW, workflow["id"], users["bob"]) is None
    # Bob now sees nothing at all: no public visibility and no grant.
    assert (
        bob_client.get(f"/api/workflows/{workflow['id']}", headers=csrf(bob_token)).status_code
        == 404
    )


def test_public_workflow_is_visible_to_another_user(app_client, alice, bob, users):
    """PUBLIC widens view access, which is what makes the previous test meaningful."""
    alice_client, alice_token = alice
    bob_client, bob_token = bob
    template_id = _template(alice_client, alice_token)
    workflow = _workflow(
        alice_client,
        alice_token,
        template_id=template_id,
        credential_id=_credential(users["alice"]),
        visibility="public",
    )
    assert (
        bob_client.get(f"/api/workflows/{workflow['id']}", headers=csrf(bob_token)).status_code
        == 200
    )


# --------------------------------------------------------------------------- #
# D3 -- log visibility enforcement
# --------------------------------------------------------------------------- #

SAME_ORIGIN = {"origin": "http://localhost:5180"}


def _seed_execution(
    owner_id: int,
    *,
    log_visibility: str = "private",
    workflow_id: int | None = None,
) -> tuple[int, int]:
    """An execution with one host run and a written transcript."""
    from app.models.enums import ExecutionStatus, HostRunStatus, LogVisibility
    from app.services import execution_state
    from app.services.execution_log import HostTranscript
    from app.services.execution_state import HostSpec, StepSpec

    execution_id = execution_state.create_execution(
        workflow_name="logs",
        steps=[StepSpec("probe", 1, "echo hi\n", "bash", 30)],
        hosts=[HostSpec("web-1.example.com", 22, 1, "lab", username="tester")],
        created_by_id=owner_id,
        workflow_id=workflow_id,
    )
    host_run_id = execution_state.get_execution(execution_id).host_runs[0].id
    with HostTranscript(
        execution_id=execution_id, host_run_id=host_run_id, hostname="web-1.example.com"
    ) as transcript:
        transcript.note("probe")
        transcript.write_stdout("hi\n")
    execution_state.finish_host(host_run_id, status=HostRunStatus.SUCCEEDED)
    execution_state.finish_execution(execution_id, status=ExecutionStatus.SUCCEEDED)

    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models.execution import Execution

    session = get_session_factory()()
    try:
        row = session.scalar(select(Execution).where(Execution.id == execution_id))
        row.log_visibility = LogVisibility(log_visibility)
        session.commit()
    finally:
        session.close()
    return execution_id, host_run_id


def _transcript_url(execution_id: int, host_run_id: int) -> str:
    return f"/api/executions/{execution_id}/hosts/{host_run_id}/transcript"


def test_private_logs_are_refused_to_another_user(app_client, alice, bob, users):
    alice_client, alice_token = alice
    bob_client, bob_token = bob
    execution_id, host_run_id = _seed_execution(users["alice"], log_visibility="private")

    assert (
        alice_client.get(
            _transcript_url(execution_id, host_run_id), headers=csrf(alice_token)
        ).status_code
        == 200
    )
    denied = bob_client.get(_transcript_url(execution_id, host_run_id), headers=csrf(bob_token))
    assert denied.status_code == 404
    assert "hi" not in denied.text


def test_public_logs_are_readable_by_another_user(app_client, alice, bob, users):
    alice_client, alice_token = alice
    bob_client, bob_token = bob
    execution_id, host_run_id = _seed_execution(users["alice"], log_visibility="public")

    assert (
        alice_client.get(
            _transcript_url(execution_id, host_run_id), headers=csrf(alice_token)
        ).status_code
        == 200
    )
    response = bob_client.get(_transcript_url(execution_id, host_run_id), headers=csrf(bob_token))
    assert response.status_code == 200, response.text
    assert "hi" in response.json()["content"]


def test_shared_logs_follow_the_workflow_grant(app_client, alice, bob, users):
    """SHARED means "whoever can already see the workflow", not "everyone"."""
    alice_client, alice_token = alice
    bob_client, bob_token = bob
    template_id = _template(alice_client, alice_token)
    workflow = _workflow(
        alice_client,
        alice_token,
        template_id=template_id,
        credential_id=_credential(users["alice"]),
    )
    execution_id, host_run_id = _seed_execution(
        users["alice"], log_visibility="shared", workflow_id=workflow["id"]
    )

    # No grant yet: Bob shares nothing with the workflow.
    assert (
        bob_client.get(
            _transcript_url(execution_id, host_run_id), headers=csrf(bob_token)
        ).status_code
        == 404
    )

    _share(workflow["id"], users["bob"], "view")
    assert (
        bob_client.get(
            _transcript_url(execution_id, host_run_id), headers=csrf(bob_token)
        ).status_code
        == 200
    )


def test_workflow_visibility_does_not_publish_logs(app_client, alice, bob, users):
    """A public workflow must not make its run output public."""
    alice_client, alice_token = alice
    bob_client, bob_token = bob
    template_id = _template(alice_client, alice_token)
    workflow = _workflow(
        alice_client,
        alice_token,
        template_id=template_id,
        credential_id=_credential(users["alice"]),
        visibility="public",
    )
    execution_id, host_run_id = _seed_execution(
        users["alice"], log_visibility="private", workflow_id=workflow["id"]
    )

    # The workflow is visible...
    assert (
        bob_client.get(f"/api/workflows/{workflow['id']}", headers=csrf(bob_token)).status_code
        == 200
    )
    # ...but its logs are not.
    assert (
        bob_client.get(
            _transcript_url(execution_id, host_run_id), headers=csrf(bob_token)
        ).status_code
        == 404
    )


def test_transcript_metadata_stays_owner_only(app_client, alice, bob, users):
    """Reading a run's transcript is separate from reading its metadata row."""
    _alice_client, _alice_token = alice
    bob_client, bob_token = bob
    execution_id, _host_run_id = _seed_execution(users["alice"], log_visibility="public")

    # Public logs, but the execution detail is still the owner's alone.
    assert (
        bob_client.get(f"/api/executions/{execution_id}", headers=csrf(bob_token)).status_code
        == 404
    )


def test_websocket_transcript_follows_log_visibility(app_client, alice, bob, users):
    """The socket must not be a softer door than the HTTP route."""
    from starlette.testclient import WebSocketDenialResponse

    from app.services.log_broadcast import get_broadcaster

    bob_client, _bob_token = bob
    broadcaster = get_broadcaster()
    broadcaster.reset()
    try:
        private_id, private_host = _seed_execution(users["alice"], log_visibility="private")
        with pytest.raises(WebSocketDenialResponse) as denied:
            with bob_client.websocket_connect(
                f"/api/executions/{private_id}/hosts/{private_host}/ws",
                headers=SAME_ORIGIN,
            ):
                pass
        assert denied.value.status_code == 404

        public_id, public_host = _seed_execution(users["alice"], log_visibility="public")
        with bob_client.websocket_connect(
            f"/api/executions/{public_id}/hosts/{public_host}/ws", headers=SAME_ORIGIN
        ) as websocket:
            assert websocket.receive_json()["type"] in {"replay", "status", "size"}
    finally:
        broadcaster.reset()
