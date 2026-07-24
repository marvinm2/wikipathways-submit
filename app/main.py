"""FastAPI application entry point.

Run locally: ``uvicorn app.main:app --reload``.

Wired: the transactional registry (WPID allocator + pathway lock), the new-pathway submission
flow, the update flow, and the curation dashboard (queue / checklist / assign / approve-merge).
Write paths (``/api/submit``, ``/api/pathways/{wpid}/update``, ``/api/reviews/{n}/approve``)
depend on a GitHub client via ``get_github_client`` and return 503 until the OAuth/App identities
(scaffolding-plan §3) are configured — read-only dashboard endpoints work without one.
"""
from __future__ import annotations

import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from app.auth import GithubOAuth, OAuthError
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

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


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


def _make_floor_provider(settings: Settings) -> Callable[[], int]:
    if settings.github_token:
        return lambda: github_wpid_floor(
            settings.content_repo_owner,
            settings.content_repo_name,
            settings.github_token,  # type: ignore[arg-type]
            branch=settings.default_branch,
        )
    # Local dev: no GitHub read, just a static floor the local reservations build on.
    return lambda: settings.dev_wpid_floor


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.database_url)
        Base.metadata.create_all(engine)  # dev convenience; production uses Alembic migrations
        session_factory = make_session_factory(engine)
        app.state.settings = settings
        app.state.session_factory = session_factory
        app.state.allocator = WpidAllocator(
            session_factory,
            _make_floor_provider(settings),
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

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(
            request, "index.html", {**_page_ctx(request), "repo": settings.content_repo}
        )

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request):
        reviews = [_detail(r).model_dump() for r in _curation(request).list_queue()]
        return templates.TemplateResponse(
            request, "dashboard.html", {**_page_ctx(request), "reviews": reviews}
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
        _curation(request).register(
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
        _curation(request).register(
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
    ):
        try:
            r = _curation(request).set_checklist_item(pr_number, key, state, note)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _detail(r)

    @app.post("/api/reviews/{pr_number}/assign", response_model=ReviewDetail)
    def assign_review(request: Request, pr_number: int, curator: str = Form(...)):
        try:
            r = _curation(request).assign(pr_number, curator)
        except ReviewNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _detail(r)

    @app.post("/api/reviews/{pr_number}/approve", response_model=ReviewDetail)
    def approve_review(
        request: Request,
        pr_number: int,
        curator: str = Depends(get_current_user),
        github: GitHubClient = Depends(get_github_client),
    ):
        try:
            r = _curation(request, github).approve_and_merge(pr_number, curator)
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
