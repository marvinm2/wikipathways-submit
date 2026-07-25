"""Application settings (12-factor: env-driven, with dev-friendly defaults)."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WPSUBMIT_", env_file=".env", extra="ignore")

    # Registry datastore. SQLite for dev; PostgreSQL in production (see scaffolding-plan §0).
    database_url: str = "sqlite:///./registry.db"

    # Target content repo the app opens PRs against.
    content_repo: str = "wikipathways/wikipathways-database"
    default_branch: str = "main"

    # TTLs for the transactional registry (design §4.2/§4.3).
    wpid_reservation_ttl_days: int = 14
    pathway_lock_ttl_days: int = 3

    # Curator whitelist (~20 people, design §4.5). Only these may approve-that-merges.
    # Preferred: resolve from a GitHub Team, WPSUBMIT_CURATOR_TEAM='org/team-slug' (issue #9) —
    # curator management then happens on GitHub, not in a redeploy (needs the App's org
    # Members:read permission). If unset, falls back to the static WPSUBMIT_CURATORS list
    # (JSON, e.g. '["alice","bob"]'), which is also used for tests / local dev.
    curator_team: str | None = None
    curators: list[str] = []

    # Bot/reader identity for server-side GitHub reads (the WPID floor). Distinct from the
    # per-user OAuth tokens used for writes. Absent in local dev → uses dev_wpid_floor.
    github_token: str | None = None
    #: Floor used when no token is configured (local dev). Real deployments read GitHub.
    dev_wpid_floor: int = 0

    # GitHub App (bot) identity, scaffolding-plan §3 — privileged merge + mirror comment, and
    # (when no github_token is set) the WPID floor read. The private key comes from a Docker
    # secret: prefer github_app_private_key_path (a mounted secret file) over an inline PEM.
    # All absent → merge/approve routes 503; mirror comments are skipped.
    github_app_id: str | None = None
    github_app_installation_id: str | None = None
    github_app_private_key: str | None = None
    github_app_private_key_path: str | None = None

    # Shared secret for verifying inbound GitHub webhooks (issue #8) — the App's webhook secret,
    # a Docker secret in production. Absent → POST /webhooks/github returns 503.
    github_webhook_secret: str | None = None

    # Per-user OAuth (submitter identity, scaffolding-plan §3). Absent → auth routes 503.
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: str | None = None
    oauth_redirect_uri: str = "http://localhost:8000/auth/callback"
    # public_repo: push branches + open PRs on the public content repo; read:user: identity.
    oauth_scope: str = "public_repo read:user"
    # Signs the session cookie holding the user token. MUST be overridden in production.
    session_secret: str = "dev-insecure-change-me"
    # Set True behind TLS so the session cookie is only sent over HTTPS (issue #4).
    session_https_only: bool = False
    # Encrypts the OAuth token at rest inside the session (issue #4). A Fernet key; if unset a
    # key is derived from session_secret. Set/rotate independently in production.
    token_encryption_key: str | None = None

    @property
    def content_repo_owner(self) -> str:
        return self.content_repo.split("/", 1)[0]

    @property
    def content_repo_name(self) -> str:
        return self.content_repo.split("/", 1)[1]
