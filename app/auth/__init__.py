"""GitHub OAuth (per-user submitter identity, scaffolding-plan §3)."""

from app.auth.github_oauth import GithubOAuth, OAuthError

__all__ = ["GithubOAuth", "OAuthError"]
