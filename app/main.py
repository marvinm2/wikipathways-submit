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
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from app.auth import GitHubApp, GithubOAuth, OAuthError
from app.config import Settings
from app.db import make_engine, make_session_factory
from app.github import GitHubClient, GitHubError, HttpGitHubClient
from app.locks import LockUnavailable, PathwayLockRegistry
from app.models import Base, ReviewStatus
from app.review.service import (
    ChecklistIncomplete,
    CurationService,
    NotACurator,
    ReviewNotFound,
)
from app.submit import InvalidGpml, SubmissionService, layout_paths, validate_gpml
from app.update import PathwayNotFound, UpdateService
from app.wpid import WpidAllocator
from app.wpid.github_floor import github_wpid_floor

_ROOT = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(_ROOT / "templates"))


def get_current_user(request: Request) -> str:
    """The authenticated GitHub login from the session. 401 if not logged in.

    Identity comes from the OAuth session, never from a client-supplied form field — a submitter
    cannot act as someone else.
    """
    login = request.session.get("gh_login")
    if not login:
        raise HTTPException(status_code=401, detail="not authenticated — GET /auth/login")
    return login


def get_github_client(request: Request) -> GitHubClient:
    """Build a GitHub client acting as the logged-in user (their OAuth token). 401 if absent."""
    token = request.session.get("gh_token")
    if not token:
        raise HTTPException(status_code=401, detail="not authenticated — GET /auth/login")
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


def _make_bot_app(settings: Settings) -> GitHubApp | None:
    """Construct the GitHub App from settings, loading the private key from PEM or a secret file."""
    if not (settings.github_app_id and settings.github_app_installation_id):
        return None
    key = settings.github_app_private_key
    if not key and settings.github_app_private_key_path:
        key = Path(settings.github_app_private_key_path).read_text()
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
            curators=settings.curators,
            allocator=st.allocator,
            locks=st.locks,
        )

    app = FastAPI(title="wikipathways-submit", version="0.0.1", lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, https_only=False)
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
            "is_curator": bool(login) and login in settings.curators,
        }

    def _review_view(r) -> dict:
        """The per-review dict the templates consume (design §4.5) — enriched beyond the API model.

        ``preview`` is the before/after render + validation artifact from the MVP-1 pipeline;
        None until that artifact is wired in (issue #6/#7), so templates show a "generating" state.
        """
        return {
            **_detail(r).model_dump(),
            "wpid_str": f"WP{r.wpid}",
            "pr_url": f"https://github.com/{settings.content_repo}/pull/{r.pr_number}",
            "preview": None,
        }

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(
            request, "index.html", {**_page_ctx(request), "repo": settings.content_repo}
        )

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request, status: ReviewStatus = ReviewStatus.OPEN):
        reviews = [_review_view(r) for r in _curation(request).list_queue(status=status)]
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                **_page_ctx(request),
                "reviews": reviews,
                "curators": settings.curators,
                "repo": settings.content_repo,
                "status": status.value,
            },
        )

    @app.get("/dashboard/{pr_number}", response_class=HTMLResponse)
    def review_page(request: Request, pr_number: int):
        try:
            r = _curation(request).get(pr_number)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request,
            "review_detail.html",
            {
                **_page_ctx(request),
                "review": _review_view(r),
                "curators": settings.curators,
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
        request.session["gh_token"] = token
        request.session["gh_login"] = login
        return RedirectResponse("/", status_code=302)

    @app.get("/auth/me")
    def auth_me(request: Request) -> dict[str, object]:
        login = request.session.get("gh_login")
        return {
            "authenticated": bool(login),
            "login": login,
            "is_curator": login in settings.curators,
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
            will_layout_to=layout_paths(0)["gpml"].replace("WP0", "WP<assigned>"),
        )

    @app.post("/api/submit", response_model=SubmitResponse, status_code=201)
    async def submit(
        request: Request,
        file: UploadFile,
        submitter: str = Depends(get_current_user),
        github: GitHubClient = Depends(get_github_client),
        bot: GitHubClient | None = Depends(get_bot_optional),
    ) -> SubmitResponse:
        content = await file.read()
        service = SubmissionService(
            request.app.state.allocator,
            github,
            repo=settings.content_repo,
            base_branch=settings.default_branch,
        )
        try:
            result = service.submit_new_pathway(gpml=content, submitter=submitter)
        except InvalidGpml as exc:
            raise HTTPException(status_code=422, detail={"errors": exc.reasons}) from exc
        except GitHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        _curation(request, bot).register(
            pr_number=result.pr_number, wpid=result.wpid, submitter=submitter, kind="new"
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
        submitter: str = Depends(get_current_user),
        github: GitHubClient = Depends(get_github_client),
        bot: GitHubClient | None = Depends(get_bot_optional),
    ) -> SubmitResponse:
        content = await file.read()
        service = UpdateService(
            request.app.state.locks,
            github,
            repo=settings.content_repo,
            base_branch=settings.default_branch,
        )
        try:
            result = service.update_pathway(wpid=wpid, gpml=content, submitter=submitter)
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
        _curation(request, bot).register(
            pr_number=result.pr_number, wpid=result.wpid, submitter=submitter, kind="update"
        )
        return SubmitResponse(
            wpid=result.wpid_str,
            pr_number=result.pr_number,
            pr_url=result.pr_url,
            path=result.path,
        )

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
        note: str = Form(""),
        actor: str = Depends(get_current_user),
        bot: GitHubClient | None = Depends(get_bot_optional),
    ):
        # Only curators mutate review state (design §4.5); non-curators get a read-only view.
        if actor not in settings.curators:
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
        if actor not in settings.curators:
            raise HTTPException(status_code=403, detail=f"{actor} is not a curator")
        try:
            r = _curation(request, bot).assign(pr_number, curator)
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
        # Merge runs as the bot (App installation token), never the curator's personal token
        # (scaffolding-plan §3) — so it can satisfy branch protection and stays attributable.
        try:
            r = _curation(request, bot).approve_and_merge(pr_number, curator)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except NotACurator as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ChecklistIncomplete as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except GitHubError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return _detail(r)

    @app.post("/api/pathways/{wpid}/release")
    async def force_release(
        request: Request, wpid: int, curator: str = Depends(get_current_user)
    ) -> dict[str, bool]:
        # Curator override (design §4.3): restricted to the curator whitelist.
        if curator not in settings.curators:
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
        if payload.get("action") != "closed":
            return {"ok": True, "ignored": f"action:{payload.get('action')}"}

        pr = payload.get("pull_request") or {}
        pr_number = payload.get("number") or pr.get("number")
        if pr_number is None:
            raise HTTPException(status_code=422, detail="no PR number in payload")
        merged = bool(pr.get("merged"))
        review = _curation(request, bot).handle_pr_closed(int(pr_number), merged=merged)
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


class ReviewSummary(BaseModel):
    pr_number: int
    wpid: int
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
