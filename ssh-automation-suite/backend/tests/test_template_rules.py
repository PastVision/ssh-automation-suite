"""Execution rules over the template API.

Covers the parts that are easy to get wrong and expensive to discover later:
a rule round-trips, a rule-only edit appends a version, a rule-free template keeps
its historic checksum, and an incoherent rule is rejected at the boundary.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.models.enums import (
    ConditionLocation,
    MatchOperator,
    OutputStream,
    StepAction,
)
from app.models.template import TemplateVersion
from app.services import templates
from app.services.conditions import Condition, StepRule
from tests.conftest import csrf, sign_in

ALICE = ("alice", "alice-password-1")


@pytest.fixture()
def alice(app_client: TestClient, users) -> tuple[TestClient, str]:
    client = TestClient(app_client.app)
    return client, sign_in(client, *ALICE)


def _step(
    name: str = "guard",
    script: str = "echo ok\n",
    rule: dict[str, object] | None = None,
) -> dict[str, object]:
    step: dict[str, object] = {
        "name": name,
        "script": script,
        "interpreter": "bash",
        "timeout_seconds": 60,
    }
    if rule is not None:
        step["rule"] = rule
    return step


def _fail_if(expected: str = "ERROR", **condition: object) -> dict[str, object]:
    return {
        "action": "fail_if",
        "condition": {
            "operator": "contains",
            "expected": expected,
            "case_sensitive": True,
            **condition,
        },
    }


def _create(client: TestClient, token: str, steps: list[dict[str, object]], name="Guarded"):
    return client.post(
        "/api/templates",
        json={"name": name, "description": "d", "visibility": "private", "steps": steps},
        headers=csrf(token),
    )


def _update(client: TestClient, token: str, template_id: int, steps: list[dict[str, object]]):
    """Save an edit, taking the edit lock the API requires first."""
    lock = client.post(f"/api/locks/template/{template_id}", headers=csrf(token))
    assert lock.status_code == 201, lock.text
    return client.put(
        f"/api/templates/{template_id}",
        json={"name": "Guarded", "description": "d", "steps": steps},
        headers=csrf(token),
    )


# --------------------------------------------------------------------------- #
# Round trip
# --------------------------------------------------------------------------- #


def test_rule_round_trips_through_create_and_read(alice):
    client, token = alice
    rule = _fail_if("CRITICAL", location="anchor", anchor="disk:")
    response = _create(client, token, [_step(rule=rule)])
    assert response.status_code == 201, response.text

    template_id = response.json()["id"]
    detail = client.get(f"/api/templates/{template_id}", headers={"X-CSRF-Token": token})
    assert detail.status_code == 200, detail.text

    stored = detail.json()["steps"][0]["rule"]
    assert stored["action"] == "fail_if"
    assert stored["condition"]["expected"] == "CRITICAL"
    assert stored["condition"]["location"] == "anchor"
    assert stored["condition"]["anchor"] == "disk:"
    assert stored["condition"]["case_sensitive"] is True


def test_step_without_a_rule_reads_back_as_continue(alice):
    client, token = alice
    response = _create(client, token, [_step()])
    template_id = response.json()["id"]

    detail = client.get(f"/api/templates/{template_id}", headers={"X-CSRF-Token": token})
    rule = detail.json()["steps"][0]["rule"]
    assert rule["action"] == "continue"
    assert rule["condition"] is None


def test_version_history_exposes_the_rule_of_each_version(alice):
    client, token = alice
    response = _create(client, token, [_step(rule=_fail_if("one"))])
    template_id = response.json()["id"]
    version_id = response.json()["versions"][0]["id"]

    version = client.get(
        f"/api/templates/{template_id}/versions/{version_id}",
        headers={"X-CSRF-Token": token},
    )
    assert version.status_code == 200, version.text
    assert version.json()["steps"][0]["rule"]["condition"]["expected"] == "one"


# --------------------------------------------------------------------------- #
# Versioning and checksums
# --------------------------------------------------------------------------- #


def test_a_rule_only_change_appends_a_version(alice):
    client, token = alice
    created = _create(client, token, [_step(rule=_fail_if("one"))])
    template_id = created.json()["id"]
    first_version_id = created.json()["versions"][0]["id"]

    updated = _update(client, token, template_id, [_step(rule=_fail_if("two"))])
    assert updated.status_code == 200, updated.text

    versions = updated.json()["versions"]
    assert len(versions) == 2
    # New identity, and the old version is untouched.
    assert versions[0]["id"] != first_version_id

    old = client.get(
        f"/api/templates/{template_id}/versions/{first_version_id}",
        headers={"X-CSRF-Token": token},
    )
    assert old.json()["steps"][0]["rule"]["condition"]["expected"] == "one"


def test_identical_rule_save_does_not_append_a_version(alice):
    client, token = alice
    created = _create(client, token, [_step(rule=_fail_if("one"))])
    template_id = created.json()["id"]

    again = _update(client, token, template_id, [_step(rule=_fail_if("one"))])
    assert again.status_code == 200, again.text
    assert len(again.json()["versions"]) == 1


def _checksum(name: str, description: str | None, steps: list[templates.StepData]) -> str:
    """Checksum of a body, going through the same path the service uses."""
    body = templates.TemplateBody(name=name, description=description, steps=tuple(steps))
    return TemplateVersion.compute_content_checksum(
        name=name, description=description, steps=body.steps_as_dicts()
    )


def test_rule_free_checksum_matches_the_pre_rules_payload():
    """A template with no rules must hash the historic payload shape.

    This is the literal dict the pre-rules milestone hashed. If the canonical form
    gains a rule key for unconditional steps, every existing template looks edited
    on its next save and appends a spurious version.
    """
    legacy_payload = [
        {
            "name": "usage",
            "description": None,
            "script": "df -h",
            "interpreter": "bash",
            "timeout_seconds": 60,
        }
    ]
    historic = TemplateVersion.compute_content_checksum(
        name="Disk check", description="check disk usage", steps=legacy_payload
    )

    body = templates.TemplateBody(
        name="Disk check",
        description="check disk usage",
        steps=(templates.StepData(name="usage", script="df -h", timeout_seconds=60),),
    )
    assert (
        TemplateVersion.compute_content_checksum(
            name=body.name,
            description=body.description,
            steps=body.steps_as_dicts(),
        )
        == historic
    )


def test_checksum_ignores_an_explicit_continue_rule():
    """Writing an explicit `continue` must not look like a content change."""
    plain = [templates.StepData(name="usage", script="df -h", timeout_seconds=60)]
    explicit = [
        templates.StepData(
            name="usage",
            script="df -h",
            timeout_seconds=60,
            rule=StepRule(action=StepAction.CONTINUE),
        )
    ]
    assert _checksum("n", None, plain) == _checksum("n", None, explicit)


def test_checksum_changes_when_a_condition_changes():
    condition = Condition(
        stream=OutputStream.STDOUT,
        location=ConditionLocation.ANYWHERE,
        operator=MatchOperator.CONTAINS,
        expected="one",
        case_sensitive=True,
    )
    one = [
        templates.StepData(
            name="usage",
            script="df -h",
            timeout_seconds=60,
            rule=StepRule(action=StepAction.FAIL_IF, condition=condition),
        )
    ]
    two = [
        templates.StepData(
            name="usage",
            script="df -h",
            timeout_seconds=60,
            rule=StepRule(
                action=StepAction.FAIL_IF,
                condition=replace(condition, expected="two"),
            ),
        )
    ]
    assert _checksum("n", None, one) != _checksum("n", None, two)


def test_a_save_that_only_adds_an_explicit_continue_creates_no_new_version(alice):
    """A UI save of an untouched pre-rules template must stay a no-op.

    The editor now always sends `rule` on every step, so a template authored
    before this milestone arrives at the API carrying an explicit `continue`.
    If that counted as a content change, opening and saving an old template would
    fabricate a version and make real history ambiguous.
    """
    client, token = alice
    created = _create(client, token, [_step()])
    assert created.status_code == 201, created.text
    template_id = created.json()["id"]
    before = client.get(f"/api/templates/{template_id}", headers=csrf(token)).json()

    # Re-open and save with no user edits, exactly as the editor does.
    lock = client.post(f"/api/locks/template/{template_id}", headers=csrf(token))
    assert lock.status_code in (200, 201), lock.text

    payload = {
        "name": before["name"],
        "description": before["description"],
        "steps": [
            {
                "name": step["name"],
                "description": step["description"],
                "script": step["script"],
                "interpreter": step["interpreter"],
                "timeout_seconds": step["timeout_seconds"],
                "rule": {"action": "continue", "condition": None},
            }
            for step in before["steps"]
        ],
        "row_version": before["row_version"],
    }
    saved = client.put(
        f"/api/templates/{template_id}", json=payload, headers=csrf(token)
    )
    assert saved.status_code == 200, saved.text

    after = client.get(f"/api/templates/{template_id}", headers=csrf(token)).json()
    assert after["current_version"] == before["current_version"]


def test_the_editors_explicit_continue_rule_is_accepted(alice):
    """The editor sends `rule: {action: "continue", condition: null}` on every step.

    It does this so the payload is uniform, and `condition` is explicitly null
    rather than absent. The API must accept that shape, otherwise every save from
    the UI fails with a 422 that names a field the author never touched.
    """
    client, token = alice
    response = _create(
        client,
        token,
        [_step(rule={"action": "continue", "condition": None})],
    )
    assert response.status_code == 201, response.text
    template_id = response.json()["id"]
    detail = client.get(f"/api/templates/{template_id}", headers=csrf(token))
    assert detail.json()["steps"][0]["rule"] == {"action": "continue", "condition": None}


def test_a_rule_without_the_case_flag_is_case_insensitive(alice):
    """The default must be the same in the schema, the service and the column.

    A client may omit `case_sensitive`. If the schema said True while the engine
    said False, the stored rule would evaluate differently from what the API
    documented, and the difference would only show up in production.
    """
    client, token = alice
    response = _create(
        client,
        token,
        [_step(rule={"action": "fail_if", "condition": {"operator": "contains", "expected": "x"}})],
    )
    assert response.status_code == 201, response.text

    template_id = response.json()["id"]
    detail = client.get(f"/api/templates/{template_id}", headers={"X-CSRF-Token": token})
    assert detail.json()["steps"][0]["rule"]["condition"]["case_sensitive"] is False


# --------------------------------------------------------------------------- #
# Rejected rules
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("rule", "reason"),
    [
        (
            {"action": "fail_if", "condition": None},
            "requires a condition",
        ),
        (
            {"action": "continue", "condition": {"operator": "contains", "expected": "x"}},
            "must not carry a condition",
        ),
        (
            {
                "action": "fail_if",
                "condition": {"operator": "contains", "expected": "x", "line_number": 3},
            },
            "anywhere",
        ),
        (
            {
                "action": "fail_if",
                "condition": {"operator": "contains", "expected": "x", "location": "anchor"},
            },
            "anchor",
        ),
        (
            {
                "action": "fail_if",
                "condition": {
                    "operator": "contains",
                    "expected": "x",
                    "location": "line_number",
                },
            },
            "line_number",
        ),
        (
            {"action": "regex_if", "condition": {"operator": "contains", "expected": "x"}},
            "action",
        ),
        (
            {
                "action": "fail_if",
                "condition": {"operator": "regex", "expected": "x"},
            },
            "operator",
        ),
    ],
)
def test_incoherent_rules_are_rejected(alice, rule, reason):
    client, token = alice
    response = _create(client, token, [_step(rule=rule)])
    assert response.status_code == 422, response.text
    assert reason in response.text


def test_unknown_rule_fields_are_rejected(alice):
    """An extra key could mean the client expects behaviour we do not implement."""
    client, token = alice
    response = _create(
        client,
        token,
        [_step(rule={"action": "fail_if", "condition": None, "on_error": "retry"})],
    )
    assert response.status_code == 422, response.text


def test_empty_expected_value_is_rejected(alice):
    client, token = alice
    response = _create(client, token, [_step(rule=_fail_if(""))])
    assert response.status_code == 422, response.text
