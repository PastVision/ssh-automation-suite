"""Authentication: login, sessions, CSRF, expiry, revocation.

These tests use two real accounts in separate cookie jars, so "another user" is a
separate browser rather than a mocked identity.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_session_factory
from app.db.types import utcnow
from tests.conftest import csrf, sign_in


def test_login_requires_correct_password(app_client: TestClient, users) -> None:
    response = app_client.post(
        "/api/auth/login", json={"username": "alice", "password": "wrong"}
    )
    assert response.status_code == 401
    # The message must not distinguish "no such user" from "wrong password".
    assert "invalid" in response.json()["detail"].lower()


def test_login_unknown_user_matches_wrong_password(app_client: TestClient, users) -> None:
    unknown = app_client.post(
        "/api/auth/login", json={"username": "nobody", "password": "x"}
    )
    wrong = app_client.post(
        "/api/auth/login", json={"username": "alice", "password": "x"}
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


def test_successful_login_sets_httponly_session_cookie(
    app_client: TestClient, users
) -> None:
    response = app_client.post(
        "/api/auth/login", json={"username": "alice", "password": "alice-password-1"}
    )
    assert response.status_code == 200

    session_cookie = response.headers.get("set-cookie", "")
    assert "sshsuite_session=" in session_cookie
    assert "HttpOnly" in session_cookie, "the session token must not be readable by JS"
    assert "SameSite=lax" in session_cookie.replace("samesite", "SameSite")

    # The CSRF cookie is readable on purpose; the header requirement is the guard.
    csrf_cookie = [
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith("sshsuite_csrf=")
    ]
    assert csrf_cookie and "HttpOnly" not in csrf_cookie[0]


def test_session_token_is_not_stored_in_plaintext(app_client: TestClient, users) -> None:
    """A database read must not yield a usable session token."""
    token = sign_in(app_client, "alice", "alice-password-1")
    assert token

    raw_token = app_client.cookies.get("sshsuite_session")
    session = get_session_factory()()
    try:
        rows = session.execute(
            __import__("sqlalchemy").text("SELECT token_hash FROM user_sessions")
        ).fetchall()
    finally:
        session.close()

    assert rows, "a session row should exist"
    for (token_hash,) in rows:
        assert token_hash != raw_token, "the raw token must never be persisted"
        assert len(token_hash) == 64, "expected a SHA-256 hex digest"


def test_me_requires_authentication(app_client: TestClient) -> None:
    assert app_client.get("/api/auth/me").status_code == 401


def test_me_returns_the_signed_in_user(app_client: TestClient, users) -> None:
    sign_in(app_client, "alice", "alice-password-1")
    body = app_client.get("/api/auth/me").json()
    assert body["user"]["username"] == "alice"
    assert body["user"]["display_name"] == "Alice"
    assert body["auth_mode"] == "local"


def test_status_is_200_when_anonymous(app_client: TestClient) -> None:
    """The SPA needs a non-error probe to decide whether to show the login page."""
    body = app_client.get("/api/auth/status").json()
    assert body == {
        "authenticated": False,
        "auth_mode": "local",
        "user": None,
        "csrf_token": None,
        "session": None,
    }


# --------------------------------------------------------------------------- #
# CSRF
# --------------------------------------------------------------------------- #

def test_state_changing_request_without_csrf_is_rejected(
    app_client: TestClient, users
) -> None:
    sign_in(app_client, "alice", "alice-password-1")
    response = app_client.patch("/api/profile", json={"ssh_username": "deploy"})
    assert response.status_code == 403
    assert "CSRF" in response.json()["detail"]


def test_state_changing_request_with_wrong_csrf_is_rejected(
    app_client: TestClient, users
) -> None:
    sign_in(app_client, "alice", "alice-password-1")
    response = app_client.patch(
        "/api/profile", json={"ssh_username": "deploy"}, headers=csrf("not-the-token")
    )
    assert response.status_code == 403


def test_csrf_cookie_alone_is_not_accepted(app_client: TestClient, users) -> None:
    """Double-submit only works if the header is required.

    The CSRF cookie is deliberately readable by JavaScript, so a browser attaches
    it to any request a hostile page can trigger. If the cookie alone satisfied the
    check, CSRF protection would be defeated.
    """
    sign_in(app_client, "alice", "alice-password-1")
    cookie_value = app_client.cookies.get("sshsuite_csrf")
    assert cookie_value

    # Sent as a cookie only (the test client already does that) - must still fail.
    response = app_client.patch("/api/profile", json={"ssh_username": "deploy"})
    assert response.status_code == 403


def test_correct_csrf_token_is_accepted(app_client: TestClient, users) -> None:
    token = sign_in(app_client, "alice", "alice-password-1")
    response = app_client.patch(
        "/api/profile", json={"ssh_username": "deploy"}, headers=csrf(token)
    )
    assert response.status_code == 200
    assert response.json()["ssh_username"] == "deploy"


def test_csrf_token_from_another_session_is_rejected(
    app_client: TestClient, users
) -> None:
    """The token is bound to the session, not just to "some logged-in user"."""
    alice_token = sign_in(app_client, "alice", "alice-password-1")

    with TestClient(app_client.app) as bob_client:
        bob_token = sign_in(bob_client, "bob", "bob-password-1")
        assert bob_token != alice_token

    response = app_client.patch(
        "/api/profile", json={"ssh_username": "x"}, headers=csrf(bob_token)
    )
    assert response.status_code == 403


def test_get_requests_are_not_csrf_gated(app_client: TestClient, users) -> None:
    sign_in(app_client, "alice", "alice-password-1")
    assert app_client.get("/api/profile").status_code == 200


def test_expired_session_cookie_does_not_block_signing_back_in(
    app_client: TestClient, users
) -> None:
    """A dead cookie must never lock a user out of the form that replaces it.

    The CSRF guard used to demand the header before validating the session. A
    browser holding an expired cookie was therefore rejected with 403 on
    `/api/auth/login` itself - the one endpoint that could clear it. Since the
    cookie grants no authority once invalid, there is nothing to forge.
    """
    sign_in(app_client, "alice", "alice-password-1")
    assert app_client.cookies.get("sshsuite_session")

    # Make the browser's cookie point at a session that no longer exists, which is
    # what a user sees after their session expires or is revoked elsewhere.
    app_client.cookies.set("sshsuite_session", "not-a-real-session-token")

    response = app_client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "alice-password-1"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["user"]["username"] == "alice"


def test_a_signed_in_browser_can_sign_in_again(
    app_client: TestClient, users
) -> None:
    """Sign-in must not require a CSRF token it cannot have.

    A still-valid session cookie used to make `/api/auth/login` answer 403, so any
    user who revisited the login page without signing out first was stuck. Login
    creates a session, so it is gated on Origin instead of on a token.
    """
    sign_in(app_client, "alice", "alice-password-1")
    response = app_client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "alice-password-1"},
        headers={"Origin": "http://localhost:5180"},
    )
    assert response.status_code == 200, response.text


def test_cross_origin_sign_in_is_rejected(app_client: TestClient, users) -> None:
    """Login CSRF: a hostile page must not log a visitor into its own account."""
    response = app_client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "alice-password-1"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert "origin" in response.json()["detail"].lower()


def test_same_origin_sign_in_is_accepted(app_client: TestClient, users) -> None:
    """The dev UI origin is allowlisted, so the browser can actually sign in."""
    response = app_client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "alice-password-1"},
        headers={"Origin": "http://127.0.0.1:5180"},
    )
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# Logout and revocation
# --------------------------------------------------------------------------- #

def test_logout_invalidates_the_session(app_client: TestClient, users) -> None:
    token = sign_in(app_client, "alice", "alice-password-1")
    assert app_client.post("/api/auth/logout", headers=csrf(token)).status_code == 204

    # The client may still hold a stale cookie; the server must not honour it.
    assert app_client.get("/api/auth/me").status_code == 401


def test_revoked_session_cannot_be_reused(app_client: TestClient, users) -> None:
    sign_in(app_client, "alice", "alice-password-1")
    stolen = app_client.cookies.get("sshsuite_session")

    from app.services import sessions

    sessions.revoke_all_for_user(users["alice"], reason="test")

    # Simulate the stolen cookie being replayed after revocation.
    replay = TestClient(app_client.app)
    replay.cookies.set("sshsuite_session", stolen)
    assert replay.get("/api/auth/me").status_code == 401


def test_inactive_user_cannot_log_in(app_client: TestClient, users) -> None:
    from app.db.session import get_session_factory
    from app.models.user import User

    session = get_session_factory()()
    try:
        user = session.get(User, users["alice"])
        user.is_active = False
        session.commit()
    finally:
        session.close()

    response = app_client.post(
        "/api/auth/login", json={"username": "alice", "password": "alice-password-1"}
    )
    assert response.status_code == 401
    assert "disabled" in response.json()["detail"].lower()


def test_inactive_user_session_is_rejected_mid_flight(
    app_client: TestClient, users
) -> None:
    """Deactivating an account must not wait for the session to expire."""
    sign_in(app_client, "alice", "alice-password-1")
    assert app_client.get("/api/auth/me").status_code == 200

    from app.db.session import get_session_factory
    from app.models.user import User

    session = get_session_factory()()
    try:
        user = session.get(User, users["alice"])
        user.is_active = False
        session.commit()
    finally:
        session.close()

    assert app_client.get("/api/auth/me").status_code == 401


def test_one_session_revocation_does_not_affect_the_other(
    app_client: TestClient, users
) -> None:
    """Two browsers, same user: revoking one must leave the other working."""
    sign_in(app_client, "alice", "alice-password-1")

    with TestClient(app_client.app) as second:
        sign_in(second, "alice", "alice-password-1")

        from app.services import sessions

        rows = sessions.list_sessions(users["alice"])
        live = [row for row in rows if row.revoked_at is None]
        assert len(live) == 2

        sessions.revoke_session(live[0].id, reason="test")

        still_ok = [row for row in sessions.list_sessions(users["alice"]) if row.revoked_at is None]
        assert len(still_ok) == 1


def test_user_cannot_revoke_another_users_session(
    app_client: TestClient, users
) -> None:
    alice_token = sign_in(app_client, "alice", "alice-password-1")

    with TestClient(app_client.app) as bob_client:
        sign_in(bob_client, "bob", "bob-password-1")

        from app.services import sessions

        alice_sessions = sessions.list_sessions(users["alice"])
        alice_session_id = alice_sessions[0].id

        response = bob_client.delete(
            f"/api/auth/sessions/{alice_session_id}",
            headers=csrf(bob_client.cookies.get("sshsuite_csrf") or ""),
        )
        # 404 rather than 403: confirm nothing about another user's session ids.
        assert response.status_code == 404

    # Alice's session must still work.
    assert app_client.get("/api/auth/me").status_code == 200
    assert alice_token


# --------------------------------------------------------------------------- #
# Expiry
# --------------------------------------------------------------------------- #

def test_idle_expiry_is_enforced(app_client: TestClient, users) -> None:
    sign_in(app_client, "alice", "alice-password-1")

    # Age the session past its idle deadline. Expiry is server-side, so the
    # browser's cookie lifetime is irrelevant.
    from sqlalchemy import update

    from app.models.user import UserSession

    session = get_session_factory()()
    try:
        session.execute(
            update(UserSession).values(expires_at=utcnow() - timedelta(seconds=1))
        )
        session.commit()
    finally:
        session.close()

    assert app_client.get("/api/auth/me").status_code == 401


def test_absolute_expiry_caps_a_renewed_session(app_client: TestClient, users) -> None:
    """An active session must still die at the absolute deadline.

    Otherwise a stolen token could be kept alive forever by polling.
    """
    sign_in(app_client, "alice", "alice-password-1")

    from sqlalchemy import update

    from app.models.user import UserSession

    session = get_session_factory()()
    try:
        # Idle deadline in the future, but created so long ago that the absolute
        # cap (12h by default) has already passed.
        session.execute(
            update(UserSession).values(
                expires_at=utcnow() + timedelta(hours=1),
                created_at=utcnow() - timedelta(hours=48),
            )
        )
        session.commit()
    finally:
        session.close()

    assert app_client.get("/api/auth/me").status_code == 401


def test_activity_extends_the_idle_deadline(app_client: TestClient, users) -> None:
    sign_in(app_client, "alice", "alice-password-1")

    from app.services import sessions

    before = sessions.list_sessions(users["alice"])[0].expires_at
    app_client.get("/api/auth/me")
    after = sessions.list_sessions(users["alice"])[0].expires_at

    from app.db.types import as_utc

    assert as_utc(after) > as_utc(before)


def test_password_verification_is_constant_time_on_unknown_users(
    app_client: TestClient, users, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both branches must do the same KDF work (no timing oracle for usernames).

    Response bodies being identical is not enough: the original implementation did
    *two* scrypt derivations for an unknown username (one to build a placeholder
    hash, one to verify against it) and one for a known account, a measurable
    ~1.8x gap that disclosed which accounts exist. Timing itself is too noisy to
    assert on, so this counts derivations, which is the actual invariant.
    """
    from app.core import passwords

    calls: list[int] = [0]
    real_derive = passwords._derive

    def counting_derive(*args, **kwargs):
        calls[0] += 1
        return real_derive(*args, **kwargs)

    monkeypatch.setattr(passwords, "_derive", counting_derive)

    known = app_client.post(
        "/api/auth/login", json={"username": "alice", "password": "bad"}
    )
    known_work = calls[0]

    calls[0] = 0
    unknown = app_client.post(
        "/api/auth/login", json={"username": "ghost", "password": "bad"}
    )
    unknown_work = calls[0]

    assert known.status_code == unknown.status_code == 401
    assert known.json() == unknown.json()
    assert known_work > 0, "expected the known-user path to run the KDF"
    assert unknown_work == known_work, (
        f"KDF work differs between known ({known_work}) and unknown "
        f"({unknown_work}) usernames, which leaks account existence by timing"
    )
