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
    # Env: WPSUBMIT_CURATORS='["alice","bob"]' (JSON list).
    curators: list[str] = []

    # Bot/reader identity for server-side GitHub reads (the WPID floor). Distinct from the
    # per-user OAuth tokens used for writes. Absent in local dev → uses dev_wpid_floor.
    github_token: str | None = None
    #: Floor used when no token is configured (local dev). Real deployments read GitHub.
    dev_wpid_floor: int = 0

    # Per-user OAuth (submitter identity, scaffolding-plan §3). Absent → auth routes 503.
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: str | None = None
    oauth_redirect_uri: str = "http://localhost:8000/auth/callback"
    # public_repo: push branches + open PRs on the public content repo; read:user: identity.
    oauth_scope: str = "public_repo read:user"
    # Signs the session cookie holding the user token. MUST be overridden in production.
    session_secret: str = "dev-insecure-change-me"

    @property
    def content_repo_owner(self) -> str:
        return self.content_repo.split("/", 1)[0]

    @property
    def content_repo_name(self) -> str:
        return self.content_repo.split("/", 1)[1]
