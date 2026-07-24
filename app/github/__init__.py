"""GitHub client abstraction used by the submission/curation flows.

The abstraction exists so the flows are unit-testable without a network (``FakeGitHubClient``)
and so the two identities from scaffolding-plan §3 — per-user OAuth for the push/PR, the
GitHub App for privileged actions — can be swapped in behind the same interface.
"""

from app.github.client import (
    BranchAlreadyExists,
    FakeGitHubClient,
    GitHubClient,
    GitHubError,
    HttpGitHubClient,
    PullRequest,
)

__all__ = [
    "BranchAlreadyExists",
    "FakeGitHubClient",
    "GitHubClient",
    "GitHubError",
    "HttpGitHubClient",
    "PullRequest",
]
