"""Pathway preview service (issue #11) — surface the before/after render to the dashboard.

The PR-preview workflow (MVP-1, issue #6) renders the changed pathway(s) to SVG and uploads them
as a run artifact. This service reads that artifact **as an external GitHub client** — it never
renders anything itself — extracts the before/after SVGs for a pathway, caches them on disk, and
lets the app serve them to the curation dashboard.

Two responsibilities, kept apart so the dashboard stays fast:

- ``status(pr_number)`` — a cheap check (no artifact download) used while rendering the queue,
  so each card shows pending / ready / failed. TTL-cached.
- ``ensure_svg(pr_number, wpid, side)`` — the heavy path (download + unzip), run lazily only when
  the browser actually requests an image, then served from the on-disk cache.

Artifact layout (from the workflow's ``preview/`` dir): the *after* (submitted/PR version) is
``WP<id>/WP<id>-after.svg`` if a dedicated renderer (e.g. PinPath) produced one, otherwise the
gpmlconverter render ``WP<id>/WP<id>.svg``; the *before* (current ``main`` version, updates only)
is ``WP<id>/WP<id>-before.svg``. Preferring ``-after.svg`` keeps before/after a consistent pair
when both frames come from the same renderer.
"""
from __future__ import annotations

import io
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.github import GitHubClient, GitHubError

_SIDES = ("before", "after")


@dataclass(frozen=True)
class PreviewState:
    status: str  # 'pending' | 'ready' | 'failed'
    has_before: bool = False
    has_after: bool = False


class PreviewService:
    def __init__(
        self,
        client_provider: Callable[[], GitHubClient | None],
        *,
        repo: str,
        cache_dir: str | Path,
        workflow_file: str,
        artifact_name: str,
        ttl_seconds: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client_provider = client_provider
        self._repo = repo
        self._cache_dir = Path(cache_dir)
        self._workflow_file = workflow_file
        self._artifact_name = artifact_name
        self._ttl = ttl_seconds
        self._clock = clock
        self._status_cache: dict[int, tuple[float, str]] = {}
        self._extract_cache: dict[int, tuple[float, PreviewState]] = {}

    # -- cheap status (queue render) -------------------------------------------------------
    def status(self, pr_number: int) -> str:
        now = self._clock()
        hit = self._status_cache.get(pr_number)
        if hit and now - hit[0] < self._ttl:
            return hit[1]
        client = self._client_provider()
        if client is None:
            return "pending"  # bot not configured → keep the "generating" state
        try:
            status = client.pr_preview_status(
                self._repo,
                pr_number,
                workflow_file=self._workflow_file,
                artifact_name=self._artifact_name,
            )
        except GitHubError:
            return "pending"
        # 'absent' (no such PR) reads as pending to the UI — nothing to show, nothing wrong.
        status = status if status in ("ready", "failed") else "pending"
        self._status_cache[pr_number] = (now, status)
        return status

    # -- heavy download + extract (image request) -----------------------------------------
    def ensure(self, pr_number: int, wpid: int) -> PreviewState:
        """Download + extract the artifact (TTL-cached) and report which sides are available."""
        now = self._clock()
        hit = self._extract_cache.get(pr_number)
        if hit and now - hit[0] < self._ttl:
            return hit[1]
        client = self._client_provider()
        if client is None:
            return PreviewState("pending")
        try:
            zip_bytes = client.download_pr_preview_zip(
                self._repo,
                pr_number,
                workflow_file=self._workflow_file,
                artifact_name=self._artifact_name,
            )
        except GitHubError:
            return PreviewState("pending")
        if not zip_bytes:
            return PreviewState("failed")
        state = self._extract(pr_number, wpid, zip_bytes)
        self._extract_cache[pr_number] = (now, state)
        return state

    def svg_path(self, pr_number: int, wpid: int, side: str) -> Path | None:
        """Cached path to the before/after SVG, downloading+extracting on first request."""
        if side not in _SIDES:
            return None
        self.ensure(pr_number, wpid)
        path = self._cache_dir / str(pr_number) / f"{side}.svg"
        return path if path.is_file() else None

    def _extract(self, pr_number: int, wpid: int, zip_bytes: bytes) -> PreviewState:
        before_name = f"WP{wpid}-before.svg"
        after_pref = f"WP{wpid}-after.svg"  # dedicated renderer (PinPath) — preferred
        after_fallback = f"WP{wpid}.svg"  # gpmlconverter render
        before: bytes | None = None
        after: bytes | None = None
        after_is_preferred = False
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                for entry in zf.namelist():
                    base = entry.rsplit("/", 1)[-1]
                    if base == before_name:
                        before = zf.read(entry)
                    elif base == after_pref:
                        after = zf.read(entry)
                        after_is_preferred = True
                    elif base == after_fallback and not after_is_preferred:
                        after = zf.read(entry)
        except zipfile.BadZipFile:
            return PreviewState("failed")
        if before is None and after is None:
            return PreviewState("failed")
        out = self._cache_dir / str(pr_number)
        out.mkdir(parents=True, exist_ok=True)
        if before is not None:
            (out / "before.svg").write_bytes(before)
        if after is not None:
            (out / "after.svg").write_bytes(after)
        return PreviewState("ready", has_before=before is not None, has_after=after is not None)
