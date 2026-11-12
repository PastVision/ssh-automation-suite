"""Application configuration.

Every setting can be overridden with an ``SSHSUITE_``-prefixed environment
variable or an entry in ``backend/.env``. Paths are derived from the backend
directory rather than the process working directory so the app behaves the
same regardless of where it is launched from.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = BACKEND_DIR / ".env"

DEFAULT_CORS_ORIGINS = ["http://localhost:5180", "http://127.0.0.1:5180"]
# ``NoDecode`` stops pydantic-settings from JSON-parsing the env value, so the
# comma-separated form below is accepted.
CorsOrigins = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SSHSUITE_",
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "SSH Automation Suite"
    app_version: str = "0.1.0"
    environment: Literal["development", "test", "production"] = "development"
    api_prefix: str = "/api"
    debug: bool = False

    cors_origins: CorsOrigins = Field(default_factory=lambda: list(DEFAULT_CORS_ORIGINS))

    data_dir: Path = BACKEND_DIR / "data"
    log_dir: Path = BACKEND_DIR / "logs"
    # When unset, the SQLite file lives at ``data_dir/ssh_suite.db``.
    database_url: str | None = None
    # When unset, the Fernet key lives at ``data_dir/secret.key``.
    secret_key_file: Path | None = None

    # Ceiling for host workflows running at the same time across all executions.
    max_concurrent_host_workflows: int = Field(default=20, ge=1, le=200)
    scheduler_timezone: str = "UTC"

    # --- Scheduler ----------------------------------------------------------
    # Off by default so a second worker, a test run, or a developer's shell never
    # competes for the lease by accident. One process in a deployment sets it.
    scheduler_enabled: bool = False
    # How often the loop looks for due firings. Sub-minute values only create
    # load; the loop is idempotent, so a slower tick delays runs but loses none.
    scheduler_poll_seconds: float = Field(default=15.0, gt=0, le=300)
    # Lateness beyond which a firing is treated as missed and the misfire policy
    # applies, instead of being dispatched immediately.
    scheduler_misfire_grace_seconds: int = Field(default=300, ge=0, le=86400)
    # Lease lifetime and renewal cadence. Must satisfy
    # ``scheduler_lease_seconds > 2 * scheduler_heartbeat_seconds`` so a holder is
    # always renewed well before it could expire.
    scheduler_lease_seconds: int = Field(default=60, ge=10, le=3600)
    scheduler_heartbeat_seconds: int = Field(default=15, ge=1, le=600)
    # Upper bound on firings recovered per schedule after downtime, so a schedule
    # that has been down for months cannot produce a dispatch storm.
    scheduler_max_catchup_per_cycle: int = Field(default=10, ge=1, le=1000)

    log_level: str = "INFO"

    # --- SSH transport ------------------------------------------------------
    ssh_connect_timeout_seconds: float = Field(default=15.0, gt=0, le=300)
    # Applied when a template version carries no usable timeout of its own.
    ssh_command_timeout_seconds: int = Field(default=300, ge=1, le=86400)
    # Verify host keys against this file. When unset, the user's standard
    # ``~/.ssh/known_hosts`` is used, which is what a real deployment should do.
    # There is deliberately no "trust everything" default.
    ssh_known_hosts_file: Path | None = None
    # Escape hatch for the disposable Docker lab, whose container host keys are
    # regenerated along with their volumes. Refused outright in production; see
    # the validator below.
    ssh_allow_unknown_host_keys: bool = False

    # --- Authentication -----------------------------------------------------
    # ``local``: application accounts with passwords (works everywhere, testable).
    # ``proxy``: a trusted authenticating reverse proxy asserts the identity.
    auth_mode: Literal["local", "proxy"] = "local"

    # Idle and absolute session lifetimes. Both are enforced server-side; the
    # cookie's own max-age is only a browser hint.
    session_idle_timeout_minutes: int = Field(default=60, ge=1, le=10080)
    session_absolute_timeout_hours: int = Field(default=12, ge=1, le=720)

    # Defaults to on in production and off elsewhere, so a local HTTP dev server
    # works without ceremony while production can never silently drop the flag.
    # Set explicitly to override; production rejects ``False`` (see validator).
    cookie_secure: bool | None = None
    cookie_name: str = "sshsuite_session"
    csrf_cookie_name: str = "sshsuite_csrf"
    csrf_header_name: str = "X-CSRF-Token"

    # --- Trusted proxy mode -------------------------------------------------
    # Only requests whose immediate peer is in one of these networks may assert
    # an identity. This is the trust boundary: anything else is treated as an
    # anonymous direct client, no matter what headers it sends.
    trusted_proxy_cidrs: CorsOrigins = Field(
        default_factory=lambda: ["127.0.0.1/32", "::1/128"]
    )
    identity_header: str = "X-Authenticated-User"
    proxy_shared_secret_header: str = "X-Proxy-Secret"
    # Shared secret the proxy must present. When unset, the header check is
    # skipped but the network check still applies.
    trust_proxy_secret: str | None = None
    # Optional: extra header carrying a stable external id (else the username is
    # used). Group membership headers are deliberately not trusted.
    identity_id_header: str | None = None

    # --- Shutdown -----------------------------------------------------------
    # How long in-flight host workflows may finish after shutdown begins.
    shutdown_grace_seconds: float = Field(default=30.0, ge=0, le=600)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("trusted_proxy_cidrs", mode="before")
    @classmethod
    def _split_cidrs(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def _validate_cidrs(cls, value: list[str]) -> list[str]:
        import ipaddress

        for item in value:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"trusted_proxy_cidrs contains an invalid network: {item}"
                ) from exc
        return value

    @field_validator("api_prefix")
    @classmethod
    def _normalise_prefix(cls, value: str) -> str:
        prefix = "/" + value.strip("/")
        return "" if prefix == "/" else prefix

    @model_validator(mode="after")
    def _resolve_paths(self) -> Settings:
        self.data_dir = self.data_dir.expanduser().resolve()
        self.log_dir = self.log_dir.expanduser().resolve()
        # A relative secret_key_file must anchor to the backend directory, not the
        # process working directory: otherwise a different launch directory yields a
        # different (newly generated) key and every stored credential becomes
        # undecryptable.
        if self.secret_key_file is not None:
            key_file = self.secret_key_file.expanduser()
            if not key_file.is_absolute():
                key_file = BACKEND_DIR / key_file
            self.secret_key_file = key_file.resolve()
        if self.ssh_known_hosts_file is not None:
            known_hosts = self.ssh_known_hosts_file.expanduser()
            if not known_hosts.is_absolute():
                known_hosts = BACKEND_DIR / known_hosts
            self.ssh_known_hosts_file = known_hosts.resolve()
        return self

    @model_validator(mode="after")
    def _guard_auth_configuration(self) -> Settings:
        """Fail closed when authentication cannot actually work.

        Three combinations are refused, because each would either leave the app
        unauthenticated in production or make proxy identity forgeable:

        * production + proxy mode with no configured trust secret or with an
          allowlist covering the whole internet;
        * production with ``cookie_secure=False``, which would send session
          cookies over plain HTTP;
        * proxy mode without any trusted proxy network, which could never
          authenticate anybody.
        """
        if self.auth_mode == "proxy":
            if not self.trusted_proxy_cidrs:
                raise ValueError(
                    "auth_mode='proxy' requires at least one entry in trusted_proxy_cidrs; "
                    "otherwise no request can ever be authenticated."
                )
            if self.environment == "production":
                if not self.trust_proxy_secret:
                    raise ValueError(
                        "auth_mode='proxy' in production requires trust_proxy_secret so a "
                        "client cannot forge the identity header."
                    )
                if any(net == "0.0.0.0/0" or net == "::/0" for net in self.trusted_proxy_cidrs):
                    raise ValueError(
                        "trusted_proxy_cidrs must not trust the whole internet in production."
                    )
        if self.environment == "production" and self.cookie_secure is False:
            raise ValueError(
                "cookie_secure must stay enabled in production: session cookies must not "
                "travel over plain HTTP."
            )
        return self

    @model_validator(mode="after")
    def _guard_host_key_policy(self) -> Settings:
        """Refuse to disable host-key verification in production.

        The lab needs unknown host keys accepted because its container keys are
        regenerated with their volumes. That must never reach production, so the
        combination is rejected at startup rather than trusted to configuration
        discipline.
        """
        if self.ssh_allow_unknown_host_keys and self.environment == "production":
            raise ValueError(
                "ssh_allow_unknown_host_keys cannot be enabled when environment='production'. "
                "Verify host keys against ssh_known_hosts_file instead."
            )
        return self

    @model_validator(mode="after")
    def _guard_scheduler_timing(self) -> Settings:
        """Refuse a lease that can expire before it is renewed.

        A lease shorter than two heartbeats would let a healthy holder be judged
        dead between renewals, which turns the single-writer guarantee into flapping
        double dispatch. Rejecting the configuration is cheaper than debugging it.
        """
        if self.scheduler_lease_seconds <= 2 * self.scheduler_heartbeat_seconds:
            raise ValueError(
                "scheduler_lease_seconds must exceed twice scheduler_heartbeat_seconds "
                f"({self.scheduler_lease_seconds} <= 2 * {self.scheduler_heartbeat_seconds})."
            )
        return self

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "ssh_suite.db"

    @property
    def resolved_cookie_secure(self) -> bool:
        """Whether session cookies carry the Secure flag.

        Unset means "on except in development", so a plain-HTTP local dev server
        can keep a session while a production deployment is secure unless it
        explicitly opts out -- which the validator above forbids.
        """
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.environment != "development"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.sqlite_path.as_posix()}"

    @property
    def resolved_secret_key_file(self) -> Path:
        return self.secret_key_file or (self.data_dir / "secret.key")

    @property
    def execution_log_dir(self) -> Path:
        return self.log_dir / "executions"

    @property
    def private_key_dir(self) -> Path:
        """Keystore holding Fernet-encrypted copies of uploaded private keys."""
        return self.data_dir / "keys"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.execution_log_dir.mkdir(parents=True, exist_ok=True)
        self.private_key_dir.mkdir(parents=True, exist_ok=True)

    def relative_path(self, path: Path) -> str:
        """Express ``path`` relative to the backend dir for storage in SQLite.

        Storing relative paths keeps the database portable between machines and
        container mounts.
        """
        try:
            return path.resolve().relative_to(BACKEND_DIR).as_posix()
        except ValueError:
            return path.resolve().as_posix()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
