from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import Settings


def test_health_ok(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()

    assert body["status"] == "ok"
    assert body["app"] == "SSH Automation Suite"
    assert body["environment"] == "test"
    assert body["database"]["ok"] is True
    assert body["database"]["url_scheme"] == "sqlite"
    assert body["database"]["error"] is None
    assert body["scheduler_enabled"] is False
    # Nothing started a scheduler in this process, so it must not claim to run one.
    assert body["scheduler"] == {
        "running": False,
        "lease_held": False,
        "generation": None,
    }


def test_health_reports_a_running_scheduler_without_starting_one(
    client: TestClient,
) -> None:
    """The endpoint observes the scheduler; it must never create one as a side effect."""
    from app.services import scheduler as scheduler_service

    scheduler_service.reset_scheduler()
    try:
        assert client.get("/api/health").json()["scheduler"]["running"] is False
        assert scheduler_service.peek_scheduler() is None

        existing = scheduler_service.get_scheduler()
        body = client.get("/api/health").json()

        assert body["scheduler"]["running"] is False  # built, but its loop is not started
        assert scheduler_service.peek_scheduler() is existing
    finally:
        scheduler_service.reset_scheduler()


def test_health_reports_concurrency_budget(client: TestClient) -> None:
    body = client.get("/api/health").json()

    assert body["concurrency"]["limit"] == 20
    assert body["concurrency"]["in_use"] == 0
    assert body["concurrency"]["available"] == 20


def test_health_reports_directories(client: TestClient, settings) -> None:
    body = client.get("/api/health").json()

    assert body["paths"]["data_dir"] == str(settings.data_dir)
    assert body["paths"]["execution_log_dir"] == str(settings.execution_log_dir)
    assert body["paths"]["private_key_dir"] == str(settings.private_key_dir)


def test_root_metadata(client: TestClient, settings: Settings) -> None:
    body = client.get("/").json()

    assert body["health"] == "/api/health"
    assert body["version"] == settings.app_version


def test_openapi_exposes_only_the_intended_surface(client: TestClient) -> None:
    """Pin the public API surface.

    This is a guard, not a formality: it fails when a route appears that nobody
    intended. Workflow CRUD, minimal SSH credential management (create/list/delete,
    the minimum needed to run a workflow), execution history and host transcripts
    are present as of the workflow-manager milestone, and schedule CRUD (plus
    pause/resume, firing history and a dry-run preview) as of the scheduling
    milestone. Credential sharing and bulk host-list management remain separate
    surfaces.
    """
    assert sorted(client.get("/openapi.json").json()["paths"]) == [
        "/api/audit",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/me",
        "/api/auth/sessions",
        "/api/auth/sessions/{session_id}",
        "/api/auth/status",
        "/api/credentials",
        "/api/credentials/{credential_id}",
        "/api/executions",
        "/api/executions/{execution_id}",
        "/api/executions/{execution_id}/cancel",
        "/api/executions/{execution_id}/hosts/{host_run_id}/transcript",
        "/api/health",
        "/api/locks/{resource_type}/{resource_id}",
        "/api/locks/{resource_type}/{resource_id}/renew",
        "/api/locks/{resource_type}/{resource_id}/shares",
        "/api/locks/{resource_type}/{resource_id}/shares/{user_id}",
        "/api/profile",
        "/api/profile/key-passphrase",
        "/api/profile/private-key",
        "/api/profile/sudo-password",
        "/api/schedules",
        "/api/schedules/preview",
        "/api/schedules/{schedule_id}",
        "/api/schedules/{schedule_id}/enabled",
        "/api/schedules/{schedule_id}/runs",
        "/api/templates",
        "/api/templates/{template_id}",
        "/api/templates/{template_id}/duplicate",
        "/api/templates/{template_id}/shares",
        "/api/templates/{template_id}/shares/{user_id}",
        "/api/templates/{template_id}/versions",
        "/api/templates/{template_id}/versions/{version_number}",
        "/api/templates/{template_id}/visibility",
        "/api/workflows",
        "/api/workflows/{workflow_id}",
        "/api/workflows/{workflow_id}/run",
    ]


def test_cors_allows_vite_dev_origin(client: TestClient) -> None:
    response = client.options(
        "/api/health",
        headers={
            "Origin": "http://localhost:5180",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5180"
