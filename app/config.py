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

    # How the target repo publishes an approved pathway (docs/sandbox-pipeline.md).
    #
    #   "direct"   — the app owns publication: it allocates the WPID, and approving merges the PR.
    #                Correct for a plain content repo (wikipathways-database, a personal fork,
    #                the demo).
    #   "pipeline" — the target repo's own Actions own publication. Approving applies the
    #                `accepted` label and stops; the repo assigns the WPID, promotes the files and
    #                closes the PR unmerged. Correct for wikipathways/sandbox-wp-db.
    #
    # The default stays "direct" so existing targets keep working unchanged.
    publish_mode: str = "direct"

    # Where the target repo's PR workflow writes its derived artifacts (pipeline mode). These are
    # read anonymously over raw.githubusercontent.com — the App is not installed on that repo and
    # the files are public. Empty drafts_repo disables the read.
    drafts_repo: str = "wikipathways/sandbox-wp.gh.io"
    drafts_branch: str = "main"
    drafts_site_base_url: str = "https://sandbox.wikipathways.org"

    # The target repo's own workflows. Advisory/display only — workflow 1 fails often enough
    # (its bridgeDb step) that it must never gate an approval.
    pipeline_workflow_file: str = "1_on_pull_request.yml"
    publish_workflow_file: str = "3a_approved_pull_request.yml"

    # Label vocabulary, as already defined in sandbox-wp-db. `accepted` and `rejected` are the
    # ones that *do* something: the repo's pr_label_dispatcher turns them into workflow runs.
    label_accepted: str = "accepted"
    label_rejected: str = "rejected"
    label_resubmitted: str = "resubmitted"
    label_new_submission: str = "new pathway submission"
    label_edited_submission: str = "edited pathway submission"
    label_author_feedback: str = "author feedback required"

    # How long to wait after labelling `accepted` before calling the publish failed.
    publish_timeout_minutes: int = 30
    # If the repo's rejection workflow never closes a rejected PR, close it ourselves after the
    # same window. Safe because rejection has no side effects worth waiting for.
    close_rejected_after_timeout: bool = True

    # Who pushes the submission branch. "user" = the submitter's own OAuth token, which is the
    # historical behaviour and only works where they have push access (a fork, the demo).
    # "bot" = the GitHub App installation, with the submitter as git commit author — required on
    # a shared repo like sandbox-wp-db, where an ordinary submitter has no push rights.
    submit_identity: str = "user"
    noreply_email_domain: str = "users.noreply.github.com"

    # Floor on how often a single review is re-checked against GitHub during the dashboard
    # reconcile pass. Reviews waiting on the repo's pipeline accumulate, so this matters.
    reconcile_min_interval_seconds: int = 30

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

    # Pathway preview (before/after render, issue #11). The app reads the SVGs the PR-preview
    # workflow uploads as a run artifact and serves them to the dashboard. Needs the bot identity
    # (Actions read); without it, previews stay in the "generating" state.
    preview_workflow_file: str = "pr-preview.yml"
    preview_artifact_name: str = "pr-preview"
    preview_cache_dir: str = "./preview-cache"
    preview_cache_ttl_seconds: int = 60
    # Refuse approve-and-merge until the PR-preview CI workflow has completed successfully
    # (design problem #1: never merge a pathway whose render/validation hasn't run green).
    require_preview_check: bool = True

    # Public URL of this app, e.g. https://curator.wikipathways.org. Used to link a GitHub
    # reviewer from the mirror comment to the dashboard page holding the before/after render —
    # CI produces no image, so that page is the only place the render exists. Unset (local dev)
    # → the comment carries no link rather than a link to somebody's localhost.
    app_base_url: str = ""

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

    @property
    def is_pipeline_mode(self) -> bool:
        return self.publish_mode == "pipeline"

    @property
    def drafts_repo_owner(self) -> str:
        return self.drafts_repo.split("/", 1)[0]

    @property
    def drafts_repo_name(self) -> str:
        return self.drafts_repo.split("/", 1)[1]
