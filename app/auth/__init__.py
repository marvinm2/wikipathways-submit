"""GitHub identities (scaffolding-plan §3): per-user OAuth + the GitHub App (bot)."""

from app.auth.github_app import GitHubApp, GitHubAppError
from app.auth.github_oauth import GithubOAuth, OAuthError

__all__ = ["GitHubApp", "GitHubAppError", "GithubOAuth", "OAuthError"]
