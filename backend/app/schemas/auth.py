"""Authentication and identity API models.

No response model in this module has a field that can carry a secret. Secret
state is reported as booleans plus safe metadata only.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=1024)


class UserRead(BaseModel):
    id: int
    username: str
    display_name: str | None = None
    is_active: bool
    last_login_at: datetime | None = None
    created_at: datetime | None = None


class SessionRead(BaseModel):
    id: int
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
    auth_source: str
    user_agent: str | None = None
    remote_addr: str | None = None


class MeResponse(BaseModel):
    """Everything the shell needs to render the signed-in state."""

    user: UserRead
    auth_mode: str
    # Present only for cookie sessions, so the browser can echo it in a header.
    csrf_token: str | None = None
    session: SessionRead | None = None
    permissions: list[str] = Field(default_factory=list)


class SessionStatus(BaseModel):
    authenticated: bool
    auth_mode: str
    user: UserRead | None = None
    csrf_token: str | None = None
    session: SessionRead | None = None
