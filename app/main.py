"""FastAPI application entry point.

Run locally: ``uvicorn app.main:app --reload``.

Wired: the transactional registry (WPID allocator + pathway lock), the new-pathway submission
flow, the update flow, and the curation dashboard (queue / checklist / assign / approve-merge).
Write paths (``/api/submit``, ``/api/pathways/{wpid}/update``, ``/api/reviews/{n}/approve``)
depend on a GitHub client via ``get_github_client`` and return 503 until the OAuth/App identities
(scaffolding-plan §3) are configured — read-only dashboard endpoints work without one.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from app.auth import GitHubApp, GithubOAuth, OAuthError, TokenCipher, TokenCipherError
from app.config import Settings
from app.curators import make_curator_registry
from app.db import make_engine, make_session_factory
from app.github import GitHubClient, GitHubError, HttpGitHubClient
from app.locks import LockUnavailable, PathwayLockRegistry
from app.models import Base, ReviewStatus
from app.pipeline import DraftsReader
from app.preview import PreviewService
from app.preview.metadata import parse_curation_metadata
from app.review.service import (
    ChecklistIncomplete,
    CurationService,
    NotACurator,
    PreviewNotReady,
    ReviewNotFound,
)
from app.submit import (
    InvalidGpml,
    NoPendingSubmission,
    SubmissionService,
    layout_paths,
    validate_gpml,
)
from app.submit.gpml import PLACEHOLDER_GPML_PATH
from app.submit.service import SubmissionMode
from app.update import PathwayNotFound, UpdateService
from app.wpid import WpidAllocator
from app.wpid.github_floor import github_wpid_floor

_ROOT = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(_ROOT / "templates"))

# Serve preview SVGs with script execution disabled (SVGs can carry <script>): a strict CSP plus
# the sandbox directive neutralises them even if a viewer opens the URL directly.
_SVG_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "Cache-Control": "public, max-age=300",
}
_PREVIEW_PLACEHOLDER = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 120" role="img" '
    b'aria-label="Preview unavailable"><rect width="200" height="120" fill="#f6f8f3"/>'
    b'<text x="100" y="63" text-anchor="middle" font-family="sans-serif" font-size="11" '
    b'fill="#7c8a82">Preview unavailable</text></svg>'
)


def get_current_user(request: Request) -> str:
    """The authenticated GitHub login from the session. 401 if not logged in.

    Identity comes from the OAuth session, never from a client-supplied form field — a submitter
    cannot act as someone else.
    """
    login = request.session.get("gh_login")
    if not login:
        raise HTTPException(status_code=401, detail="not authenticated. Log in at /auth/login")
    return login


def get_github_client(request: Request) -> GitHubClient:
    """Build a GitHub client acting as the logged-in user (their OAuth token). 401 if absent."""
    encrypted = request.session.get("gh_token")
    if not encrypted:
        raise HTTPException(status_code=401, detail="not authenticated. Log in at /auth/login")
    try:
        token = request.app.state.token_cipher.decrypt(encrypted)
    except TokenCipherError as exc:
        # Key rotated / tampered cookie: force a fresh login rather than 500.
        request.session.clear()
        raise HTTPException(
            status_code=401, detail="your session expired, log in again"
        ) from exc
    return HttpGitHubClient(token)


def get_bot_optional(request: Request) -> GitHubClient | None:
    """A GitHub client acting as the App (bot), or None if the App is not configured.

    Privileged, cross-cutting actions (merge, read-only mirror comment) run as the bot — never
    as a submitter's or curator's personal token (scaffolding-plan §3). Used where the bot is
    optional (mirror comments are best-effort); ``get_bot_client`` is the strict variant.
    """
    bot_app: GitHubApp | None = request.app.state.bot_app
    if bot_app is None:
        return None
    return HttpGitHubClient(bot_app.installation_token())


def get_bot_client(request: Request) -> GitHubClient:
    """The bot GitHub client; 503 if the GitHub App identity is not configured."""
    client = get_bot_optional(request)
    if client is None:
        raise HTTPException(
            status_code=503, detail="GitHub App (bot) identity is not configured"
        )
    return client


def _writer_client(
    settings: Settings, user_client: GitHubClient, bot: GitHubClient | None
) -> GitHubClient:
    """Whose credentials push the branch and open the PR.

    On a shared target repo an ordinary submitter has no push access, so the bot has to do it;
    authorship survives because the commit carries the submitter as its git author. Where the
    submitter *can* push (a fork, the demo), their own token is better — the PR is then genuinely
    theirs.
    """
    if settings.submit_identity != "bot":
        return user_client
    if bot is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "submit_identity=bot, but the GitHub App (bot) identity is not configured. "
                "Submitters cannot push to the target repo without it."
            ),
        )
    return bot


def _submitter_email(settings: Settings, submitter: str) -> str:
    """GitHub's noreply address, so a bot-pushed commit still shows the submitter as author."""
    return f"{submitter}@{settings.noreply_email_domain}"


def _label_submission(
    settings: Settings, bot: GitHubClient | None, pr_number: int, *, kind: str
) -> None:
    """Tag the PR with the target repo's own new/edit vocabulary.

    Best-effort on purpose: these labels are descriptive, not the mechanism, so a repo that has
    not defined them (or a bot without Issues:write) must not cost anyone their submission.
    Contrast the `accepted` label, where a failure has to fail the call.
    """
    if bot is None or not settings.is_pipeline_mode:
        return
    label = (
        settings.label_new_submission if kind == "new" else settings.label_edited_submission
    )
    try:
        bot.add_labels(settings.content_repo, pr_number, [label])
    except (GitHubError, httpx.HTTPError):
        pass


def _make_bot_app(settings: Settings) -> GitHubApp | None:
    """Construct the GitHub App from settings, loading the private key from PEM or a secret file."""
    if not (settings.github_app_id and settings.github_app_installation_id):
        return None
    key = settings.github_app_private_key
    if not key and settings.github_app_private_key_path:
        # A configured-but-unreadable key must not take the whole service down. Everywhere else
        # an absent bot identity degrades to 503 on the routes that need it, and a deployment
        # that sets the path before creating the secret should behave the same way rather than
        # crash-looping on startup with the site already live.
        try:
            key = Path(settings.github_app_private_key_path).read_text()
        except OSError:
            logging.getLogger("wpsubmit.auth").error(
                "GitHub App private key is not readable at %s; the bot identity is disabled "
                "and the routes that need it will return 503",
                settings.github_app_private_key_path,
            )
            return None
    if not key:
        return None
    return GitHubApp(
        settings.github_app_id, key, settings.github_app_installation_id
    )


def _make_floor_provider(settings: Settings, bot_app: GitHubApp | None) -> Callable[[], int]:
    if settings.github_token:
        return lambda: github_wpid_floor(
            settings.content_repo_owner,
            settings.content_repo_name,
            settings.github_token,  # type: ignore[arg-type]
            branch=settings.default_branch,
        )
    if bot_app is not None:
        # The bot's installation token also reads the repo tree for the WPID floor (issue #3).
        return lambda: github_wpid_floor(
            settings.content_repo_owner,
            settings.content_repo_name,
            bot_app.installation_token(),
            branch=settings.default_branch,
        )
    # Local dev: no GitHub read, just a static floor the local reservations build on.
    return lambda: settings.dev_wpid_floor


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.database_url)
        # Dev convenience only. Production (Postgres) runs `alembic upgrade head` on deploy — see
        # docs/migrations.md — so we never auto-create tables outside SQLite.
        if settings.database_url.startswith("sqlite"):
            Base.metadata.create_all(engine)
        session_factory = make_session_factory(engine)
        # GitHub App (bot) identity — privileged merge/comment. None → those routes 503 (dev).
        bot_app = _make_bot_app(settings)
        app.state.settings = settings
        app.state.session_factory = session_factory
        app.state.bot_app = bot_app
        app.state.token_cipher = TokenCipher(
            encryption_key=settings.token_encryption_key,
            session_secret=settings.session_secret,
        )

        def bot_provider() -> GitHubClient | None:
            return HttpGitHubClient(bot_app.installation_token()) if bot_app else None

        # Curator whitelist: a GitHub Team if WPSUBMIT_CURATOR_TEAM is set, else the config list.
        app.state.curators = make_curator_registry(
            team=settings.curator_team,
            config_logins=settings.curators,
            bot_client_provider=bot_provider,
        )
        # Pathway preview: the app draws before/after itself and caches it (issue #11).
        app.state.preview = PreviewService(cache_dir=settings.preview_cache_dir)
        # The target repo's own derived artifacts (pipeline mode). Read anonymously — the App is
        # not installed on the site repo and does not need to be, since those files are public.
        app.state.drafts = (
            DraftsReader(
                repo=settings.drafts_repo,
                branch=settings.drafts_branch,
                site_base_url=settings.drafts_site_base_url,
                cache_dir=str(Path(settings.preview_cache_dir) / "drafts"),
                ttl_seconds=settings.preview_cache_ttl_seconds,
            )
            if settings.is_pipeline_mode and settings.drafts_repo
            else None
        )
        app.state.allocator = WpidAllocator(
            session_factory,
            _make_floor_provider(settings, bot_app),
            ttl=timedelta(days=settings.wpid_reservation_ttl_days),
        )
        app.state.locks = PathwayLockRegistry(
            session_factory,
            ttl=timedelta(days=settings.pathway_lock_ttl_days),
        )
        # Per-user OAuth (writes act as the submitter). None if unconfigured → auth routes 503.
        app.state.oauth = (
            GithubOAuth(
                settings.github_oauth_client_id,
                settings.github_oauth_client_secret,
            )
            if settings.github_oauth_client_id and settings.github_oauth_client_secret
            else None
        )
        yield

    def _curation(request: Request, github: GitHubClient | None = None) -> CurationService:
        # Review CRUD needs no GitHub client; only approve_and_merge does.
        st = request.app.state
        return CurationService(
            st.session_factory,
            github,
            repo=settings.content_repo,
            curators=st.curators,
            allocator=st.allocator,
            locks=st.locks,
            require_preview_check=settings.require_preview_check,
            preview_workflow_file=settings.preview_workflow_file,
            preview_artifact_name=settings.preview_artifact_name,
            app_base_url=settings.app_base_url,
            publish_mode=settings.publish_mode,
            default_branch=settings.default_branch,
            pipeline_workflow_file=settings.pipeline_workflow_file,
            label_accepted=settings.label_accepted,
            label_rejected=settings.label_rejected,
            label_author_feedback=settings.label_author_feedback,
            publish_timeout=timedelta(minutes=settings.publish_timeout_minutes),
            close_rejected_after_timeout=settings.close_rejected_after_timeout,
            reconcile_min_interval=timedelta(
                seconds=settings.reconcile_min_interval_seconds
            ),
            drafts=st.drafts,
        )

    def _fetch_base_gpml(github: GitHubClient, path: str) -> bytes | None:
        """The current base-``main`` version of ``path``, or None (best-effort — used as the
        update 'before' for both the render and the changed-since-base checklist scoping)."""
        try:
            return github.get_file_content(settings.content_repo, settings.default_branch, path)
        except Exception:  # noqa: BLE001 — a missing/unreadable base only costs the before-view
            return None

    def _render_preview(
        request: Request,
        *,
        pr_number: int,
        wpid: int,
        after_gpml: bytes,
        before_gpml: bytes | None = None,
        submitter_note: str | None = None,
    ) -> None:
        """Instantly render the before/after preview at PR-creation time (issue #11, 1a).

        Best-effort — a render failure only costs the preview, never the submission, so the whole
        thing is swallowed (the CI artifact / placeholder still covers the frame).
        """
        try:
            request.app.state.preview.render_local(
                pr_number,
                wpid,
                after_gpml=after_gpml,
                before_gpml=before_gpml,
                submitter_note=submitter_note,
            )
        except Exception:  # noqa: BLE001 — preview is cosmetic; never fail the write path on it
            logging.getLogger("wpsubmit.preview").warning(
                "local preview render failed for PR #%s", pr_number, exc_info=True
            )

    app = FastAPI(title="wikipathways-submit", version="0.0.1", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        https_only=settings.session_https_only,
        same_site="lax",
    )
    app.mount(
        "/static",
        StaticFiles(directory=str(_ROOT / "static"), check_dir=False),
        name="static",
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # -- Pages -----------------------------------------------------------------------------

    def _page_ctx(request: Request) -> dict:
        login = request.session.get("gh_login")
        return {
            "request": request,
            "login": login,
            "is_curator": request.app.state.curators.is_curator(login),
            # Every page's footer names the target repo, so it belongs in the shared context
            # rather than in each route that happens to remember to pass it.
            "repo": settings.content_repo,
            # Changes what approval *means* on screen: a label handed to the repository, not a
            # merge this app performs.
            "pipeline_mode": settings.is_pipeline_mode,
        }

    def _review_view(request: Request, r) -> dict:
        """The per-review dict the templates consume (design §4.5) — enriched beyond the API model.

        ``preview`` is the app's own before/after render (issue #11): a cheap disk check here;
        the SVG bytes stream from ``/previews/...`` when the browser requests them. Every status
        gets it — a curator reading a change request, or checking what was merged, wants the
        diagram just as much as one reviewing an open PR.
        """
        pr_url = f"https://github.com/{settings.content_repo}/pull/{r.pr_number}"
        preview = None
        status = request.app.state.preview.status(r.pr_number)
        if status == "ready":
            preview = {
                "status": "ready",
                "before_svg_url": f"/previews/{r.pr_number}/before.svg",
                "after_svg_url": f"/previews/{r.pr_number}/after.svg",
                "datanodes_url": f"{pr_url}/files",
                "validation_url": f"{pr_url}/checks",
            }
        elif status == "failed":
            preview = {"status": "failed"}
        # 'pending' → leave None so the template shows the "generating" empty state
        return {
            **_detail(r).model_dump(),
            "wpid_str": r.wpid_str,
            "pr_url": pr_url,
            "preview": preview,
            # Parsed curation metadata (data nodes, references, description, ontology tags,
            # submitter note) cached at render time — a cheap disk read, None if not rendered.
            "metadata": request.app.state.preview.metadata(r.pr_number),
            "labels": r.github_labels or [],
            "decision_note": r.decision_note,
            "pipeline": _pipeline_view(request, r),
        }

    def _pipeline_view(request: Request, r) -> dict | None:
        """What the target repo's own pipeline has produced for this PR, if anything.

        The repo renders the pathway, resolves its identifiers and its references, and publishes
        a draft page, which is richer than anything the app makes for itself. It also fails more
        often than it succeeds: of the last 20 runs of its PR workflow, 5 succeeded and 14 failed
        (measured 2026-07-27). So every field here is optional, and the template has to read as
        "the app's own render still works" when all of them are absent.
        """
        drafts = request.app.state.drafts
        if drafts is None:
            return None
        slug = drafts.slug_for(kind=r.kind, wpid=r.wpid, pr_number=r.pr_number)
        artifacts = drafts.fetch(slug)
        run = r.pipeline_run or {}
        conclusion = run.get("conclusion")
        return {
            "slug": slug,
            "available": artifacts.available,
            "draft_url": artifacts.draft_url,
            "svg_url": artifacts.svg_url,
            "thumb_url": artifacts.thumb_url,
            "datanode_count": len(artifacts.datanodes or []) or None,
            "reference_count": len(artifacts.bibliography or []) or None,
            "run_status": run.get("status"),
            "run_conclusion": conclusion,
            "run_url": run.get("url"),
            # The case worth naming: the repository ran and could not finish. Almost always it
            # could not read the GPML, which costs the submitter their metadata and preview and
            # is otherwise only visible several clicks into the Actions tab.
            "run_failed": conclusion not in (None, "success"),
        }

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(
            request, "index.html", {**_page_ctx(request), "repo": settings.content_repo}
        )

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        status: ReviewStatus = ReviewStatus.OPEN,
        bot: GitHubClient | None = Depends(get_bot_optional),
    ):
        # Terminalise any review whose PR was closed/merged outside the app before rendering the
        # queue, so the dashboard never shows a PR that no longer exists (issue #1).
        _curation(request, bot).reconcile_open_reviews()
        reviews = [_review_view(request, r) for r in _curation(request).list_queue(status=status)]
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                **_page_ctx(request),
                "reviews": reviews,
                "curators": sorted(request.app.state.curators.members()),
                "repo": settings.content_repo,
                "status": status.value,
            },
        )

    @app.get("/previews/{pr_number}/{side}.svg")
    def preview_svg(request: Request, pr_number: int, side: str):
        # Serve the app's cached before/after render (issue #11). SVGs are served with a
        # locked-down CSP + sandbox so a hostile SVG can't run script if opened directly; the
        # dashboard only ever loads them via <img> (which already can't run script).
        if side not in ("before", "after"):
            raise HTTPException(status_code=404, detail="unknown preview side")
        try:
            _curation(request).get(pr_number)  # unknown PR → 404, not a placeholder
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        path = request.app.state.preview.svg_path(pr_number, side)
        if path is None:
            # Missing side (e.g. a new pathway has no "before", or the render is unavailable):
            # a placeholder keeps the frame intact instead of a broken-image icon.
            return Response(
                content=_PREVIEW_PLACEHOLDER, media_type="image/svg+xml", headers=_SVG_HEADERS
            )
        return FileResponse(path, media_type="image/svg+xml", headers=_SVG_HEADERS)

    @app.get("/dashboard/{pr_number}", response_class=HTMLResponse)
    def review_page(
        request: Request, pr_number: int, bot: GitHubClient | None = Depends(get_bot_optional)
    ):
        curation = _curation(request, bot)
        try:
            curation.get(pr_number)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # This page is reached directly as often as it is reached from the queue — a link in a
        # comment, a refresh after acting — so it cannot rely on a queue load having run.
        curation.reconcile_review(pr_number)
        # Fill in whatever the target repo's pipeline has already worked out, so the curator is
        # confirming answers rather than deriving them. Best-effort, and it never overwrites a
        # state a curator set by hand.
        meta = request.app.state.preview.metadata(pr_number)
        # The *cited* count, not the total: the target repo's generator only emits references a
        # <BiopaxRef> actually points at, so comparing its output against every PublicationXref
        # in the file reports a shortfall that is not one — and that item is required, so the
        # false FAIL would block approval and read as the submitter's fault.
        curation.refresh_pipeline_checks(
            pr_number,
            gpml_reference_count=getattr(meta, "cited_reference_count", None) if meta else None,
        )
        # Read the row last. Both calls above write to it, and rendering the copy fetched before
        # them would show the page as it was one refresh ago.
        r = curation.get(pr_number)
        return templates.TemplateResponse(
            request,
            "review_detail.html",
            {
                **_page_ctx(request),
                "review": _review_view(request, r),
                "curators": sorted(request.app.state.curators.members()),
                "repo": settings.content_repo,
            },
        )

    # -- Auth (GitHub OAuth) ---------------------------------------------------------------

    def _oauth(request: Request) -> GithubOAuth:
        oauth = request.app.state.oauth
        if oauth is None:
            raise HTTPException(status_code=503, detail="GitHub OAuth is not configured")
        return oauth

    @app.get("/auth/login")
    def auth_login(request: Request):
        oauth = _oauth(request)
        state = secrets.token_urlsafe(24)
        request.session["oauth_state"] = state  # CSRF guard, verified on callback
        url = oauth.authorize_url(settings.oauth_redirect_uri, state, settings.oauth_scope)
        return RedirectResponse(url, status_code=302)

    @app.get("/auth/callback")
    def auth_callback(request: Request, code: str, state: str):
        oauth = _oauth(request)
        if not state or state != request.session.get("oauth_state"):
            raise HTTPException(status_code=400, detail="OAuth state mismatch")
        request.session.pop("oauth_state", None)
        try:
            token = oauth.exchange_code(code, settings.oauth_redirect_uri)
            login = oauth.get_login(token)
        except OAuthError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        # Store the token encrypted at rest — the signed cookie is readable, encryption is not.
        request.session["gh_token"] = request.app.state.token_cipher.encrypt(token)
        request.session["gh_login"] = login
        return RedirectResponse("/", status_code=302)

    @app.get("/auth/me")
    def auth_me(request: Request) -> dict[str, object]:
        login = request.session.get("gh_login")
        return {
            "authenticated": bool(login),
            "login": login,
            "is_curator": request.app.state.curators.is_curator(login),
        }

    @app.post("/auth/logout")
    def auth_logout(request: Request) -> dict[str, bool]:
        request.session.clear()
        return {"ok": True}

    @app.post("/api/validate", response_model=ValidateResponse)
    async def validate(file: UploadFile) -> ValidateResponse:
        content = await file.read()
        try:
            meta = validate_gpml(content)
        except InvalidGpml as exc:
            raise HTTPException(status_code=422, detail={"errors": exc.reasons}) from exc
        return ValidateResponse(
            name=meta.name,
            organism=meta.organism,
            embedded_wpid=meta.wpid,
            # In pipeline mode the file is committed under a placeholder and the target repo
            # renames it at publication, so promising "WP<assigned>" here would be a lie.
            will_layout_to=(
                PLACEHOLDER_GPML_PATH
                if settings.is_pipeline_mode
                else layout_paths(0)["gpml"].replace("WP0", "WP<assigned>")
            ),
        )

    @app.post("/api/submit", response_model=SubmitResponse, status_code=201)
    async def submit(
        request: Request,
        file: UploadFile,
        description: str = Form(""),
        submitter: str = Depends(get_current_user),
        github: GitHubClient = Depends(get_github_client),
        bot: GitHubClient | None = Depends(get_bot_optional),
    ) -> SubmitResponse:
        content = await file.read()
        service = SubmissionService(
            request.app.state.allocator,
            _writer_client(settings, github, bot),
            repo=settings.content_repo,
            base_branch=settings.default_branch,
            mode=SubmissionMode(settings.publish_mode),
        )
        try:
            result = service.submit_new_pathway(
                gpml=content,
                submitter=submitter,
                author_email=_submitter_email(settings, submitter),
                description=description,
            )
        except InvalidGpml as exc:
            raise HTTPException(status_code=422, detail={"errors": exc.reasons}) from exc
        except GitHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        after_meta = parse_curation_metadata(content)
        _curation(request, bot).register(
            pr_number=result.pr_number,
            wpid=result.wpid,
            submitter=submitter,
            kind="new",
            metadata=after_meta,  # pre-fills the checklist with auto-derived states
            head_branch=result.branch,
        )
        _label_submission(settings, bot, result.pr_number, kind="new")
        _render_preview(
            request,
            pr_number=result.pr_number,
            wpid=result.wpid,
            after_gpml=content,
            before_gpml=None,  # new pathway has no base version
            submitter_note=description,
        )
        return SubmitResponse(
            wpid=result.wpid_str,
            pr_number=result.pr_number,
            pr_url=result.pr_url,
            path=result.path,
        )

    @app.post("/api/pathways/{wpid}/update", response_model=SubmitResponse, status_code=201)
    async def update(
        request: Request,
        wpid: int,
        file: UploadFile,
        description: str = Form(""),
        submitter: str = Depends(get_current_user),
        github: GitHubClient = Depends(get_github_client),
        bot: GitHubClient | None = Depends(get_bot_optional),
    ) -> SubmitResponse:
        content = await file.read()
        writer = _writer_client(settings, github, bot)
        service = UpdateService(
            request.app.state.locks,
            writer,
            repo=settings.content_repo,
            base_branch=settings.default_branch,
        )
        try:
            result = service.update_pathway(
                wpid=wpid,
                gpml=content,
                submitter=submitter,
                author_email=_submitter_email(settings, submitter),
                description=description,
            )
        except InvalidGpml as exc:
            raise HTTPException(status_code=422, detail={"errors": exc.reasons}) from exc
        except LockUnavailable as exc:
            raise HTTPException(
                status_code=409, detail={"reason": exc.reason, "held_by": exc.held_by}
            ) from exc
        except PathwayNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except GitHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        # Fetch the base version once — it is both the render 'before' and the baseline the
        # checklist uses to skip checks for things this update didn't change.
        before_gpml = _fetch_base_gpml(writer, result.path)
        after_meta = parse_curation_metadata(content)
        before_meta = parse_curation_metadata(before_gpml) if before_gpml else None
        _curation(request, bot).register(
            pr_number=result.pr_number,
            wpid=result.wpid,
            submitter=submitter,
            kind="update",
            metadata=after_meta,
            before_metadata=before_meta,
            head_branch=result.branch,
        )
        _label_submission(settings, bot, result.pr_number, kind="update")
        _render_preview(
            request,
            pr_number=result.pr_number,
            wpid=result.wpid,
            after_gpml=content,
            before_gpml=before_gpml,
            submitter_note=description,
        )
        return SubmitResponse(
            wpid=result.wpid_str,
            pr_number=result.pr_number,
            pr_url=result.pr_url,
            path=result.path,
        )

    @app.post("/api/reviews/{pr_number}/revise", response_model=SubmitResponse, status_code=201)
    async def revise_review(
        request: Request,
        pr_number: int,
        file: UploadFile,
        description: str = Form(""),
        submitter: str = Depends(get_current_user),
        github: GitHubClient = Depends(get_github_client),
        bot: GitHubClient | None = Depends(get_bot_optional),
    ) -> SubmitResponse:
        """Commit a revised GPML onto an open new-pathway PR and re-open its review.

        Keyed by pull request, not by WPID: in pipeline mode a new submission has no id until
        the target repo publishes it, so there is nothing to look it up by until then.
        """
        content = await file.read()
        curation = _curation(request, bot)
        try:
            review = curation.get(pr_number)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if review.kind != "new":
            raise HTTPException(
                status_code=409,
                detail="this is an update, not a new submission; upload it as an update instead",
            )
        if review.submitter != submitter and not request.app.state.curators.is_curator(submitter):
            raise HTTPException(
                status_code=403, detail="only the submitter or a curator can revise this submission"
            )
        service = SubmissionService(
            request.app.state.allocator,
            _writer_client(settings, github, bot),
            repo=settings.content_repo,
            base_branch=settings.default_branch,
            mode=SubmissionMode(settings.publish_mode),
        )
        try:
            result = service.revise_new_pathway(
                gpml=content,
                submitter=submitter,
                wpid=review.wpid,
                branch=review.head_branch,
                author_email=_submitter_email(settings, submitter),
            )
        except InvalidGpml as exc:
            raise HTTPException(status_code=422, detail={"errors": exc.reasons}) from exc
        except NoPendingSubmission as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except GitHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        # Re-open the review and rebuild its checklist from the revised content.
        curation.revise(result.pr_number, metadata=parse_curation_metadata(content))
        _render_preview(
            request,
            pr_number=result.pr_number,
            wpid=result.wpid,
            after_gpml=content,
            before_gpml=None,
            submitter_note=description,
        )
        return SubmitResponse(
            wpid=result.wpid_str,
            pr_number=result.pr_number,
            pr_url=result.pr_url,
            path=result.path,
        )

    @app.get("/api/pathways/{wpid}", response_model=PathwayInfo)
    def pathway_info(
        request: Request,
        wpid: int,
        github: GitHubClient = Depends(get_github_client),
    ) -> PathwayInfo:
        """Where does ``WP<wpid>`` live — on the base branch (an update), an open new-submission PR
        (a revise), or nowhere? Backs the update form's presence check and revise routing."""
        wpid_str = f"WP{wpid}"
        path = layout_paths(wpid)["gpml"]
        try:
            content = github.get_file_content(
                settings.content_repo, settings.default_branch, path
            )
        except GitHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if content is not None:
            return PathwayInfo(
                exists=True,
                state="on_main",
                wpid=wpid_str,
                name=parse_curation_metadata(content).name,
            )
        # Not on main — is there still an open new-submission PR to revise?
        try:
            pr = github.find_open_pr(settings.content_repo, f"submit/{wpid_str}")
        except GitHubError:
            pr = None
        if pr is not None:
            return PathwayInfo(
                exists=False, state="pending_new", wpid=wpid_str, pr_number=pr.number
            )
        return PathwayInfo(exists=False, state="absent", wpid=wpid_str)

    # -- Curation dashboard (MVP-4) --------------------------------------------------------

    @app.get("/api/reviews", response_model=list[ReviewSummary])
    def list_reviews(request: Request, status: ReviewStatus = ReviewStatus.OPEN):
        return [_summary(r) for r in _curation(request).list_queue(status=status)]

    @app.get("/api/reviews/{pr_number}", response_model=ReviewDetail)
    def get_review(request: Request, pr_number: int):
        try:
            r = _curation(request).get(pr_number)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _detail(r)

    @app.post("/api/reviews/{pr_number}/checklist", response_model=ReviewDetail)
    def update_checklist(
        request: Request,
        pr_number: int,
        key: str = Form(...),
        state: str = Form(...),
        # Omitted (the dashboard's state chips send no note) → the stored note is left alone.
        note: str | None = Form(None),
        actor: str = Depends(get_current_user),
        bot: GitHubClient | None = Depends(get_bot_optional),
    ):
        # Only curators mutate review state (design §4.5); non-curators get a read-only view.
        if not request.app.state.curators.is_curator(actor):
            raise HTTPException(status_code=403, detail=f"{actor} is not a curator")
        try:
            r = _curation(request, bot).set_checklist_item(pr_number, key, state, note)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _detail(r)

    @app.post("/api/reviews/{pr_number}/assign", response_model=ReviewDetail)
    def assign_review(
        request: Request,
        pr_number: int,
        curator: str = Form(...),
        actor: str = Depends(get_current_user),
        bot: GitHubClient | None = Depends(get_bot_optional),
    ):
        if not request.app.state.curators.is_curator(actor):
            raise HTTPException(status_code=403, detail=f"{actor} is not a curator")
        try:
            r = _curation(request, bot).assign(pr_number, curator)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _detail(r)

    @app.post("/api/reviews/{pr_number}/request-changes", response_model=ReviewDetail)
    def request_changes(
        request: Request,
        pr_number: int,
        note: str = Form(""),
        actor: str = Depends(get_current_user),
        bot: GitHubClient | None = Depends(get_bot_optional),
    ):
        if not request.app.state.curators.is_curator(actor):
            raise HTTPException(status_code=403, detail=f"{actor} is not a curator")
        try:
            r = _curation(request, bot).request_changes(pr_number, actor, note)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _detail(r)

    @app.post("/api/reviews/{pr_number}/approve", response_model=ReviewDetail)
    def approve_review(
        request: Request,
        pr_number: int,
        curator: str = Depends(get_current_user),
        bot: GitHubClient = Depends(get_bot_client),
    ):
        # Approval runs as the bot (App installation token), never the curator's personal token
        # (scaffolding-plan §3) — merging that way satisfies branch protection, and labelling
        # that way works even for a curator without write access to the target repo.
        try:
            r = _curation(request, bot).approve(pr_number, curator)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except NotACurator as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ChecklistIncomplete as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PreviewNotReady as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except GitHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return _detail(r)

    @app.post("/api/reviews/{pr_number}/reject", response_model=ReviewDetail)
    def reject_review(
        request: Request,
        pr_number: int,
        note: str = Form(""),
        curator: str = Depends(get_current_user),
        bot: GitHubClient = Depends(get_bot_client),
    ):
        # Terminal, unlike request-changes: in pipeline mode this hands the PR to the target
        # repo's rejection workflow, which deletes the generated drafts and closes it.
        try:
            r = _curation(request, bot).reject(pr_number, curator, note)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except NotACurator as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except GitHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return _detail(r)

    @app.post("/api/reviews/{pr_number}/published-wpid", response_model=ReviewDetail)
    def record_published_wpid(
        request: Request,
        pr_number: int,
        wpid: int = Form(...),
        curator: str = Depends(get_current_user),
        bot: GitHubClient | None = Depends(get_bot_optional),
    ):
        # The manual way out of PUBLISH_FAILED: the target repo's publish workflow is the one
        # part of the loop the app does not control, so a curator has to be able to say what it
        # actually did.
        try:
            r = _curation(request, bot).record_published_wpid(pr_number, wpid, curator)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except NotACurator as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return _detail(r)

    @app.post("/api/pathways/{wpid}/release")
    async def force_release(
        request: Request, wpid: int, curator: str = Depends(get_current_user)
    ) -> dict[str, bool]:
        # Curator override (design §4.3): restricted to the curator whitelist.
        if not request.app.state.curators.is_curator(curator):
            raise HTTPException(status_code=403, detail=f"{curator} is not a curator")
        released = request.app.state.locks.release(wpid, curator, force=True)
        return {"released": released}

    # -- GitHub webhook (issue #8): release the lock when a PR is closed/merged outside the app --

    @app.post("/webhooks/github")
    async def github_webhook(
        request: Request, bot: GitHubClient | None = Depends(get_bot_optional)
    ) -> dict[str, object]:
        secret = settings.github_webhook_secret
        if not secret:
            raise HTTPException(status_code=503, detail="webhook secret is not configured")
        raw = await request.body()
        # Verify HMAC-SHA256 over the raw body before trusting anything in it.
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=401, detail="invalid webhook signature")

        event = request.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return {"ok": True, "pong": True}
        if event != "pull_request":
            return {"ok": True, "ignored": f"event:{event}"}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid JSON") from exc
        action = payload.get("action")
        if action not in ("closed", "labeled", "unlabeled"):
            return {"ok": True, "ignored": f"action:{action}"}

        pr = payload.get("pull_request") or {}
        pr_number = payload.get("number") or pr.get("number")
        if pr_number is None:
            raise HTTPException(status_code=422, detail="no PR number in payload")
        curation = _curation(request, bot)

        if action in ("labeled", "unlabeled"):
            # A curator may well reach for the label on GitHub rather than the dashboard — those
            # labels are the target repo's own vocabulary. Recording it here is what keeps the
            # two venues from diverging.
            label = ((payload.get("label") or {}).get("name")) or ""
            actor = (payload.get("sender") or {}).get("login") or "github"
            review = curation.handle_label_event(
                int(pr_number), label, added=(action == "labeled"), actor=actor
            )
            return {
                "ok": True,
                "pr_number": int(pr_number),
                "label": label,
                "applied": review is not None,
            }

        merged = bool(pr.get("merged"))
        review = curation.handle_pr_closed(int(pr_number), merged=merged)
        return {
            "ok": True,
            "pr_number": int(pr_number),
            "merged": merged,
            "tracked": review is not None,
        }

    return app


class ValidateResponse(BaseModel):
    name: str | None
    organism: str | None
    embedded_wpid: str | None
    will_layout_to: str


class SubmitResponse(BaseModel):
    wpid: str
    pr_number: int
    pr_url: str
    path: str


class PathwayInfo(BaseModel):
    exists: bool  # present on the base branch (an update target)
    wpid: str
    name: str | None = None
    state: str = "absent"  # "on_main" | "pending_new" | "absent"
    pr_number: int | None = None  # the open new-submission PR, when state == pending_new


class ReviewSummary(BaseModel):
    pr_number: int
    #: None until the target repo assigns one (pipeline mode); see Review.wpid.
    wpid: int | None = None
    submitter: str
    kind: str
    status: str
    assigned_curator: str | None


class ReviewDetail(ReviewSummary):
    checklist: list[dict]
    approved_by: str | None


def _summary(r) -> ReviewSummary:
    return ReviewSummary(
        pr_number=r.pr_number,
        wpid=r.wpid,
        submitter=r.submitter,
        kind=r.kind,
        status=r.status.value,
        assigned_curator=r.assigned_curator,
    )


def _detail(r) -> ReviewDetail:
    return ReviewDetail(
        **_summary(r).model_dump(),
        checklist=r.checklist,
        approved_by=r.approved_by,
    )


app = build_app()
