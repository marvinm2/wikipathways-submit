# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

**MVP-2 in progress — the transactional core is built and tested.** Read `docs/design-proposal.md`
(the "why", grounded in a 3-month audit of 51 PRs) and `docs/scaffolding-plan.md` (the build
blueprint) first; they are authoritative over any assumption — keep them in sync as you build.
This is deliberately kept **local** — we do **not** file PRs against the upstream `wikipathways`
repos.

### What exists now

- `mvp1/` + `fork-staging/` — MVP-1 PR-preview pipeline (two GitHub Actions workflows +
  `validate_pathway.py`), adversarially reviewed and hardened. Ships to a **fork** of
  `wikipathways-database`; `fork-staging/CHECKLIST.md` is the test procedure. See `mvp1/README.md`.
- `app/` — the FastAPI app (MVP-2 → MVP-4). Implemented + tested (147 tests): the transactional
  registry (`app/wpid/` atomic allocator, `app/locks/` pathway check-out lock — both with
  threaded race tests), app-owned GPML naming/layout (`app/submit/gpml.py`), the `GitHubClient`
  abstraction (`app/github/` — ABC + `FakeGitHubClient` + httpx impl), the **submission service**
  (`app/submit/service.py`), the **update flow** (`app/update/service.py`, lock →
  branch-off-latest → PR, reuses an open PR on re-upload), and the **curation dashboard**
  (`app/review/` — `Review` model, checklist template, `CurationService`: queue / checklist /
  assign / approve-that-merges gated to the curator whitelist, cascading reservation→MERGED +
  lock release). All write paths roll back their WPID/lock on GitHub failure. Endpoints under
  `/api/*` (validate, submit, pathways/{wpid}/update, pathways/{wpid}/release, reviews[/{n}][/
  checklist|assign|approve]).
  **GitHub OAuth is wired** (`app/auth/`, `/auth/login|callback|logout|me`): writes act as the
  logged-in user (`get_current_user` reads the session, never a form field), and endpoints return
  **401** when not logged in. Configure it per `docs/oauth-setup.md` (register a GitHub OAuth App,
  set `WPSUBMIT_*` env vars).
  **GitHub App (bot) identity is wired** (`app/auth/github_app.py`, issue #1): RS256 JWT →
  cached installation token; the **merge** (`approve_and_merge`) and the **read-only PR mirror
  comment** (`render_mirror_comment` / `upsert_issue_comment`, best-effort — swallows both
  `GitHubError` and `httpx.HTTPError` so a comment blip never fails an already-merged action)
  run as the bot via `get_bot_client` (503 if unconfigured) / `get_bot_optional`, never a
  curator's personal token. The bot's installation token also feeds the WPID floor when no
  `WPSUBMIT_GITHUB_TOKEN` is set (issue #3). Configure per `docs/github-app-setup.md`.
  **The GitHub webhook is wired** (issue #8): `POST /webhooks/github` verifies HMAC-SHA256
  (`WPSUBMIT_GITHUB_WEBHOOK_SECRET`) and, on a `pull_request` `closed` event, releases the lock
  + finalises the reservation (MERGED if merged, returned to the pool if closed unmerged) +
  terminalises the review — idempotent, so a PR closed *outside* the app no longer waits for the
  TTL. TTL tuning against real behaviour remains open.
  **The before/after pathway preview is wired** (issue #11, `app/preview/`), two sources with the
  **in-app renderer preferred**: (1) **instant in-app render** (`app/preview/render.py`, 1a) — a
  dependency-free GPML→SVG drawer runs at PR-creation time (`render_local`, wired into submit +
  update via `_render_preview`), rendering the uploaded GPML as *after* and the base-`main` GPML
  (fetched via the new `GitHubClient.get_file_content`) as *before*, cached to disk so the preview
  is ready immediately with no CI wait; (2) fallback — the SVGs the PR-preview workflow uploads as
  a run artifact (via the bot's Actions read), `WP<id>-after.svg`/`WP<id>.svg` + `WP<id>-before.svg`.
  Both serve at `GET /previews/{pr}/{before,after}.svg` (locked-down CSP + sandbox so a hostile SVG
  can't run script). `_review_view` fills the dashboard `preview` slot from a cheap
  `PreviewService.status()`; bytes stream lazily.
  **CI draws no image at all** (2026-07-27): PinPath was retired once 1a existed, and
  `pr-preview.yml` now converts to **pvjson only** — a GPML `gpml2pvjson` refuses is broken, so
  the `.json` is a validity signal, not a picture. A PR comment cannot embed an artifact anyway,
  and camo refuses SVG, so an image in the PR would need deployment plus a PNG endpoint. That
  makes source (2) dead code — remove it when convenient. Marvin's call: the PR does not need an
  image; it carries the validation and metadata tables, and `WPSUBMIT_APP_BASE_URL` (when set)
  links the mirror comment to the dashboard page that holds the render.
  **Alembic is wired** (issue #2): `migrations/` + `alembic.ini`; `create_all` now runs **only**
  for SQLite dev, Postgres deploys run `alembic upgrade head` (`docs/migrations.md`); a test
  asserts zero drift between the migration and the models. Checklist/assign endpoints are now
  curator-gated (403 for non-curators), matching approve.
  The **dashboard/landing UI was redesigned** (issue #7, `templates/` + `static/app.{css,js}`,
  server-rendered Jinja + vanilla JS, served from `/static`): landing/submit stepper, curation
  queue with before/after preview slots, reviewer assignment, per-review detail page.
  **Curator whitelist resolves from a GitHub Team** (issue #9, `app/curators.py`,
  `WPSUBMIT_CURATOR_TEAM='org/slug'`): TTL-cached, fail-closed, `WPSUBMIT_CURATORS` list is the
  fallback. **OAuth token is encrypted at rest** (issue #4, `app/auth/session_tokens.py`, Fernet)
  and `SessionMiddleware` `https_only` is config-driven. **Cluster deployment is authored** (issue
  #5, `Dockerfile` + `docker-entrypoint.sh` + `.github/workflows/{ci,docker-publish}.yml` +
  `docker-compose.yml` + `docs/deployment.md`; image builds/boots, not yet deployed live).
  **Remaining open issues: #8's TTL tuning** (needs real submitter data) and **#14** (clickable
  data nodes in the preview — properties pop-up/side panel from the pvjson + `-datanodes.tsv`).
  Everything is verified against `FakeGitHubClient` (tests override `get_github_client`,
  `get_bot_client`/`get_bot_optional`, `get_current_user`); the OAuth + App token flows are
  tested via injected `httpx.MockTransport`.

### Commands

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"   # one-time setup
.venv/bin/python -m pytest tests/                     # run all tests
.venv/bin/python -m pytest tests/test_wpid_allocator.py::test_concurrent_allocation_no_collisions  # single test
.venv/bin/ruff check app/ tests/                      # lint
.venv/bin/uvicorn app.main:app --reload               # run the app locally
```

The allocator/lock atomicity rests on the **WPID/pathway being the table primary key** — a
concurrent duplicate insert fails with `IntegrityError` and the caller retries. Do not "optimize"
this into a compute-then-insert without the unique constraint; the race tests exist to catch that.

## What this is

`wikipathways-curator` (provisional name) is a **hosted web app that is the front door for
submitting and curating WikiPathways pathways** now that all content lives on GitHub in
[`wikipathways/wikipathways-database`](https://github.com/wikipathways/wikipathways-database).
It lets anyone submit or update a pathway without touching git: it opens a real pull request
against the content repo, assigns the WPID, and gives curators a review dashboard with a
rendered before/after preview.

The app talks to `wikipathways-database` **purely through the GitHub API as an external client**.
The only code that ships *into* the content repo is one added Actions workflow
(`pr-preview.yml`) that renders + validates GPML on `pull_request`. Do not conflate the two
repos: this repo is the app; the content repo stays a content repo.

## The five problems this solves (from the PR audit)

Every design decision traces to one of these observed failures — preserve the mapping when
changing the design:

1. **Reviewers approve unreadable XML** — reviewable artifacts (SVG, `-datanodes.tsv`,
   `-bibliography.tsv`, validation) are only generated *after* merge → fixed by the PR-preview
   pipeline (MVP-1).
2. **Manual out-of-band merges** → fixed by dashboard approve-that-merges.
3. **Unmergeable concurrent GPML edits** (GPML is XML + layout, does not line-merge) → fixed by
   the check-out lock + never line-merging GPML + always branching off latest `main`.
4. **Malformed new submissions** (no WPID, wrong filename) → fixed by app-owned naming/layout.
5. **WPID collisions** (next-id computed only over the merged tree) → fixed by the atomic
   allocator computing `1 + max(WPID)` over **repo tree ∪ open PRs ∪ live reservations**.

## Architecture (as planned)

- **Two GitHub identities, deliberately** (`app/auth/`):
  - **Per-user OAuth** — pushes the branch / opens the PR *as the submitter*, so authorship is
    real and attributed, and the user never runs git.
  - **GitHub App (bot)** — privileged cross-cutting actions the user token must not do: posting
    the preview comment, merging on curator approval, receiving webhooks (PR opened/closed → to
    expire locks).
- **The transactional core is the registry** (`app/models/`, `migrations/`) — this is the one
  place that cannot be sloppy:
  - `wpid_reservation` — allocation is an `INSERT` of `max+1` (over tree ∪ open PRs ∪ live
    reservations) **inside one transaction**, so simultaneous submissions cannot collide.
    Unmerged reservations expire and return the ID to the pool.
  - `pathway_lock` — one open edit per pathway; acquiring is a conditional upsert that **also
    scans GitHub for an open PR touching that pathway** and refuses if one exists (power users
    can bypass the app with a raw PR). Locks auto-expire; curators can force-release.
  - `review` — dashboard approval state; the single source of truth the read-only PR comment
    mirrors.
- **Two review venues, one source of truth:** the app dashboard is the reviewer's home
  (before/after render, checklist, approve-that-merges); the same preview + checklist is
  mirrored as a **read-only** PR comment on GitHub. Approval always flows through the app so the
  two never diverge.
- **Merge model:** GPML is the single source of truth and is **never line-merged**; derived
  files (`*.json`, `*.md`, `*.tsv`, `*-thumb.png`) are regenerated, never hand-reconciled.

## Locked stack decisions (from scaffolding-plan §0)

- **Backend:** Python + **FastAPI** (async, OpenAPI, clean OAuth/webhook handling).
- **GitHub client:** `githubkit` or `PyGithub` + `httpx`.
- **Datastore:** **PostgreSQL** + SQLAlchemy + Alembic (SQLite acceptable for MVP-2 dev only).
- **Frontend:** server-rendered templates + light JS; defer any SPA until the MVP-4 dashboard
  warrants it.
- **Tooling:** `uv` + `ruff` + `pytest`.
- **Deploy:** Docker → GHCR → Strato cluster service, Traefik-routed, GlusterFS-backed data.

The proposed `app/` layout (auth, github, wpid, locks, submit, review, models) is in
scaffolding-plan §1 — follow it when scaffolding.

## Build phasing — build in this order, each phase independently shippable

- **MVP-1** — PR-preview pipeline. Ships to `wikipathways-database` as `pr-preview.yml`, **not
  this repo**. Reuses the existing render/metadata generators from `on_gpml_change.yml` (subset:
  render + datanodes + refs + validation), writes a preview artifact + PR comment, does **not**
  commit derived files or push to sister repos. Highest leverage, smallest build, no app needed —
  do this first, in parallel with scaffolding.
- **MVP-2** — Submission app for new pathways: OAuth, atomic WPID allocator (write the race test),
  naming/layout, PR creation, metadata capture.
- **MVP-3** — Updates + check-out lock, branch-off-latest.
- **MVP-4** — Curation dashboard, checklist, approve-that-merges, reviewer auto-assignment,
  curator whitelist (~20 people; GitHub Team vs repo-tracked config is an open decision).

## Deployment context

This deploys to the VHP4Safety Strato Docker Swarm cluster (see the user's global instructions
and the cluster docs at `/mnt/gluster/documentation/` on `tgx1`). Follow cluster conventions:
image built by CI → GHCR so both swarm nodes can pull (real failover), `core` overlay network,
GlusterFS-backed data at `/mnt/gluster/docker/<service>/data`, **no node pinning**, secrets as
Docker secrets (never in the repo). The app needs a GitHub App identity installed on
`wikipathways-database` with contents RW, pull_requests RW, and issues/comments RW.

## Open decisions (still unresolved — scaffolding-plan §0, proposal §9)

- Final repo name (`wikipathways-curator` vs `wikipathways-submit` vs `pathway-portal`).
- License (likely Apache-2.0 to match org software norm; content is CC0 but this is code).
- Curator whitelist mechanism: GitHub Team vs repo-tracked config file.
- Bot merge vs `main` branch protection interaction.
- Lock / reservation TTLs (tune against real submitter behaviour).
