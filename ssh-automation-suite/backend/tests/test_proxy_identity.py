"""Trusted-proxy identity mode.

The security claim under test is narrow and important: an identity header is
honoured **only** when the request provably came from the configured proxy. Every
other request is refused -- never silently treated as anonymous, never treated as
the asserted user.

Peer addresses come from ``TestClient(client=(...))``, so these are real loopback
IPs and the ``ipaddress`` allowlist check runs exactly as it would in production.
Validators only run on construction, so every test builds a new ``Settings`` rather
than mutating one in place.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings


def make_settings(settings: Settings, **overrides) -> Settings:
    """A new, validated ``Settings`` derived from the test defaults."""
    values = {
        "auth_mode": "proxy",
        "trusted_proxy_cidrs": ["127.0.0.1/32"],
        "cookie_secure": False,
        "environment": "test",
    }
    values.update(overrides)
    # Rebuild from the base values so field/model validators actually run: mutating
    # an existing instance would bypass them and let a broken config through.
    return Settings(
        _env_file=None,
        data_dir=settings.data_dir,
        log_dir=settings.log_dir,
        secret_key_file=settings.secret_key_file,
        **values,
    )


def proxy_client(settings: Settings, peer: str = "127.0.0.1", **overrides):
    """A client whose peer address is ``peer`` (defaults to loopback)."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.migration_support import ensure_migrated_database

    ensure_migrated_database(settings.sqlite_path)
    configured = overrides.pop("settings_obj", None) or make_settings(settings, **overrides)
    return TestClient(create_app(configured), client=(peer, 50000))


HEADER = "X-Authenticated-User"


# --------------------------------------------------------------------------- #
# Configuration guards (these must fail closed)
# --------------------------------------------------------------------------- #

def test_proxy_mode_requires_a_trusted_network(settings: Settings) -> None:
    with pytest.raises(ValueError, match="trusted_proxy_cidrs"):
        make_settings(settings, trusted_proxy_cidrs=[])


def test_production_proxy_mode_requires_a_shared_secret(settings: Settings) -> None:
    """Without a secret a co-located client could impersonate anybody."""
    with pytest.raises(ValueError, match="trust_proxy_secret"):
        make_settings(
            settings,
            environment="production",
            cookie_secure=True,
            trusted_proxy_cidrs=["10.0.0.0/8"],
            trust_proxy_secret=None,
        )


def test_production_rejects_trusting_the_whole_internet(settings: Settings) -> None:
    with pytest.raises(ValueError, match="whole internet"):
        make_settings(
            settings,
            environment="production",
            cookie_secure=True,
            trusted_proxy_cidrs=["0.0.0.0/0"],
            trust_proxy_secret="s3cret",
        )


def test_production_requires_secure_cookies(settings: Settings) -> None:
    with pytest.raises(ValueError, match="cookie_secure"):
        make_settings(settings, auth_mode="local", environment="production",
                      cookie_secure=False)


def test_invalid_cidr_is_rejected(settings: Settings) -> None:
    with pytest.raises(ValueError, match="invalid network"):
        make_settings(settings, trusted_proxy_cidrs=["not-a-network"])


# --------------------------------------------------------------------------- #
# Anti-spoofing: the central claim
# --------------------------------------------------------------------------- #

def test_header_from_untrusted_peer_is_ignored(settings: Settings) -> None:
    """A request the allowlist does not cover must be refused outright."""
    with proxy_client(settings, peer="203.0.113.9", trusted_proxy_cidrs=["10.0.0.0/8"]) as client:
        response = client.get("/api/auth/me", headers={HEADER: "admin"})
        assert response.status_code == 401

        from app.services import sessions

        assert sessions.get_user_by_username("admin") is None, (
            "a forged header must not provision an account"
        )


def test_untrusted_peer_attempt_is_audited(settings: Settings) -> None:
    from app.services import audit

    with proxy_client(settings, peer="203.0.113.9", trusted_proxy_cidrs=["10.0.0.0/8"]) as client:
        client.get("/api/auth/me", headers={HEADER: "admin"})

    denied = [e for e in audit.list_events(limit=20) if e.action == "authz.access_denied"]
    assert denied, "a spoofing attempt must be recorded"
    assert "untrusted_peer" in (denied[0].detail or "")


def test_local_mode_ignores_the_identity_header(settings: Settings) -> None:
    with proxy_client(
        settings, auth_mode="local", trusted_proxy_cidrs=["127.0.0.1/32"]
    ) as client:
        assert client.get("/api/auth/me", headers={HEADER: "alice"}).status_code == 401


def test_trusted_peer_is_authenticated(settings: Settings) -> None:
    with proxy_client(settings) as client:
        response = client.get("/api/auth/me", headers={HEADER: "carol"})
        assert response.status_code == 200
        assert response.json()["user"]["username"] == "carol"
        assert response.json()["auth_mode"] == "proxy"


def test_empty_identity_header_is_rejected(settings: Settings) -> None:
    with proxy_client(settings) as client:
        assert client.get("/api/auth/me", headers={HEADER: "   "}).status_code == 401


def test_request_without_the_header_is_anonymous(settings: Settings) -> None:
    """Being on a trusted network is not itself an identity."""
    with proxy_client(settings) as client:
        assert client.get("/api/auth/me").status_code == 401


# --------------------------------------------------------------------------- #
# Shared secret
# --------------------------------------------------------------------------- #

def test_trusted_peer_without_the_secret_is_refused(settings: Settings) -> None:
    with proxy_client(settings, trust_proxy_secret="proxy-secret") as client:
        assert client.get("/api/auth/me", headers={HEADER: "alice"}).status_code == 401

        ok = client.get(
            "/api/auth/me",
            headers={HEADER: "alice", "X-Proxy-Secret": "proxy-secret"},
        )
        assert ok.status_code == 200
        assert ok.json()["user"]["username"] == "alice"


def test_wrong_proxy_secret_is_rejected(settings: Settings) -> None:
    with proxy_client(settings, trust_proxy_secret="proxy-secret") as client:
        response = client.get(
            "/api/auth/me", headers={HEADER: "alice", "X-Proxy-Secret": "guess"}
        )
        assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Provisioning
# --------------------------------------------------------------------------- #

def test_user_is_provisioned_once_and_reused(settings: Settings) -> None:
    with proxy_client(settings) as client:
        first = client.get("/api/auth/me", headers={HEADER: "carol"})
        second = client.get("/api/auth/me", headers={HEADER: "carol"})
        assert first.json()["user"]["id"] == second.json()["user"]["id"]


def test_external_id_keeps_identity_stable_across_renames(settings: Settings) -> None:
    """A stable external id lets the display name change without orphaning data."""
    with proxy_client(settings, identity_id_header="X-Authenticated-User-Id") as client:
        first = client.get(
            "/api/auth/me",
            headers={HEADER: "carol", "X-Authenticated-User-Id": "ext-42"},
        )
        assert first.status_code == 200

        renamed = client.get(
            "/api/auth/me",
            headers={HEADER: "carol.renamed", "X-Authenticated-User-Id": "ext-42"},
        )
        assert renamed.json()["user"]["id"] == first.json()["user"]["id"]


def test_password_login_is_unavailable_in_proxy_mode(settings: Settings) -> None:
    with proxy_client(settings) as client:
        response = client.post(
            "/api/auth/login", json={"username": "a", "password": "b"}
        )
        assert response.status_code == 409


def test_planted_session_cookie_does_not_authenticate(settings: Settings) -> None:
    """Proxy mode derives identity from the header, never from a cookie."""
    with proxy_client(settings) as client:
        client.cookies.set("sshsuite_session", "attacker-supplied")
        assert client.get("/api/auth/me").status_code == 401


def test_inactive_proxy_user_is_refused(settings: Settings) -> None:
    from app.db.session import get_session_factory
    from app.models.user import User

    with proxy_client(settings) as client:
        user_id = client.get("/api/auth/me", headers={HEADER: "dave"}).json()["user"]["id"]

        session = get_session_factory()()
        try:
            user = session.get(User, user_id)
            user.is_active = False
            session.commit()
        finally:
            session.close()

        assert client.get("/api/auth/me", headers={HEADER: "dave"}).status_code == 401


def test_each_proxy_user_gets_a_separate_profile(settings: Settings) -> None:
    with proxy_client(settings) as client:
        carol = client.get("/api/auth/me", headers={HEADER: "carol"}).json()["user"]["id"]
        dave = client.get("/api/auth/me", headers={HEADER: "dave"}).json()["user"]["id"]
        assert carol != dave

        profile = client.get("/api/profile", headers={HEADER: "carol"}).json()
        assert profile["user_id"] == carol
