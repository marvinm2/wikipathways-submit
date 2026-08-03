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
- `app/` — the FastAPI app (MVP-2 → MVP-4). Implemented + tested (287 tests): the transactional
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
  is ready immediately with no CI wait. Serves at `GET /previews/{pr}/{before,after}.svg`
  (locked-down CSP + sandbox so a hostile SVG can't run script); `_review_view` fills the
  dashboard `preview` slot from a cheap disk-based `PreviewService.status()`.
  **CI draws no image at all** (2026-07-27): PinPath was retired once 1a existed, and
  `pr-preview.yml` now converts to **pvjson only** — a GPML `gpml2pvjson` refuses is broken, so
  the `.json` is a validity signal, not a picture. A PR comment cannot embed an artifact anyway,
  and camo refuses SVG, so an image in the PR would need deployment plus a PNG endpoint. The
  app's old artifact-download path was **removed** with it, so `PreviewService` no longer talks to
  GitHub at all. Marvin's call: the PR does not need an image; it carries the validation and metadata tables, and `WPSUBMIT_APP_BASE_URL` (when set)
  links the mirror comment to the dashboard page that holds the render.
  **Alembic is wired** (issue #2): `migrations/` + `alembic.ini`; `create_all` now runs **only**
  for SQLite dev, Postgres deploys run `alembic upgrade head` (`docs/migrations.md`); a test
  asserts zero drift between the migration and the models. Checklist/assign endpoints are now
  curator-gated (403 for non-curators), matching approve.
  The **dashboard/landing UI was redesigned** (issue #7, `templates/` + `static/app.{css,js}`,
  server-rendered Jinja + vanilla JS, served from `/static`): landing/submit stepper, curation
  queue with before/after preview slots, reviewer assignment, per-review detail page. The review
  card lives in `templates/_review_card.html` and is imported `with context` by both pages —
  importing it from `dashboard.html` executed that page's body and rendered its empty state
  against a context with no queue in it.
  **Curator whitelist resolves from a GitHub Team** (issue #9, `app/curators.py`,
  `WPSUBMIT_CURATOR_TEAM='org/slug'`): TTL-cached, fail-closed, `WPSUBMIT_CURATORS` list is the
  fallback. **OAuth token is encrypted at rest** (issue #4, `app/auth/session_tokens.py`, Fernet)
  and `SessionMiddleware` `https_only` is config-driven. **Cluster deployment is authored** (issue
  #5, `Dockerfile` + `docker-entrypoint.sh` + `.github/workflows/{ci,docker-publish}.yml` +
  `docker-compose.yml` + `docs/deployment.md`; image builds/boots, not yet deployed live).
  **The before/after preview says what changed** (issue #24, `app/preview/diff.py`): every data
  node is classified added / removed / re-annotated / relabelled / moved by matching GraphId,
  then label plus type, then database plus identifier, cached as `diff.json` and served at
  `GET /previews/{pr}/diff.json`. The card carries the count sentence server-rendered; the
  overlay colours each hotspot and the panel strikes the previous value through. The overlay is
  also **one tab stop, not one per node** (issue #19): a roving tabindex under a toolbar role,
  arrow keys in reading order, selection following focus into a polite live region.
  **Quality control is one graded ruleset** (`app/quality/`, 2026-08-03). Before it there were
  five, in four vocabularies, and the richest of them never ran: `mvp1/validate_pathway.py`
  grades thirteen checks but ships inside `pr-preview.yml`, which the live target repository has
  never had. `app/quality/rules.py` holds the union — the four reasons `validate_gpml` refuses a
  file for (kept **word for word**: they reach a submitter through `describeError`), the GPML-side
  checks from `mvp1`, and the target repo's own `testing` job (title >= 10 chars, description
  >= 15 words or an edit changing it by <= 3 words / 10 chars, data-node changes) ported rule for
  rule and flagged `predicts_repo`. Severities are `na < pass < warn < fail < block`; `na` ranks
  *below* pass so "nothing to check" cannot win a rollup. **The package must import nothing from
  `app.*` at module scope** — `app.models` imports `app.review.checklist`, which imports this —
  so metadata is duck-typed and the one call into `app.submit.gpml` is function-local; an AST test
  pins it. `validate_gpml` is now defined as the `block` subset, so the portal cannot refuse a
  file for a reason its own report called fine. The report is **cached in the render sidecar**
  (`quality.json`), never persisted: it is a pure function of the GPML and the checklist is
  already the record. Surfaces: `/api/validate` (**which nothing called before** — the submit form
  now posts to it on file choice, so a submitter sees warnings before the pull request exists),
  one "Automated checks" block on the review card that absorbed the old free-floating
  pipeline-failure notice, and a table in the mirror comment. `_render_preview` therefore runs
  **before** `register`, or the first mirror comment has the table missing.
  **The checklist is aligned with the repository's own reviewer checklist** — added
  `interactions_connected`, gave `description_ok` an auto_check. That one can never return `pass`:
  `refresh_pipeline_checks` only writes items still `pending`, so anything the app puts there
  pre-empts the repo's own description check, which is strictly better (it quotes what its
  extractor pulled out, the text that reaches the published page).
  **The repo's `testing` verdicts are read back** off a `<!-- wikipathways-testing … -->` marker
  comment (`parse_testing_marker`), the same device 3a's publish marker already uses, and shown
  beside the app's predictions — a disagreement means the ported thresholds have drifted. The
  workflow step that posts it is staged in `sandbox-workflows/`, **not proposed**, so the field is
  empty on the live target until it is.
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

## Current state (2026-08-03) — read `docs/session-handoff-2026-08-03.md` first

That is the read-me-first handoff. It supersedes the 07-29 one, which remains the account of the
deployment, the fork's draft pipeline and the first publication. In short, since then: quality
control was consolidated into one graded ruleset (`app/quality/`) that runs at upload time and is
mirrored to the pull request; the app's checklist was aligned with the reviewer checklist the
target repository appends to every pull request; and the two systems can now read each other's
verdicts over a marker comment, proven end to end on the fork. A missing root `<Graphics>` canvas
was identified as a hard crasher for the repository's `metadata` job and is now a `fail` rule.

Then, in a second round the same day, three of the five open audit issues were closed: the render
cache is freed at every terminal transition and swept as a backstop (#18 — it had been leaking on
`_settle_publication`, which in pipeline mode is *how a submission succeeds*, and a second
unswept cache turned up beside it under `preview-cache/drafts`); the curation queue pages at
twenty (#17); and one account may open ten pull requests an hour, counted out of the `review`
table so it survives a redeploy (#21). **#22** (fork-per-submitter) and **#23** (TTL tuning)
remain open — the first needs a decision and a broader OAuth scope, the second needs real
submission data that does not exist yet.

437 tests. **Live is still `sha256:8d8dda3e…`, which predates all three** — nothing from the
second round is deployed.

The 07-29 summary below is kept because its details still hold.

### Previously (2026-07-29)

The whole submit → review → approve → publish lifecycle has been driven against live GitHub,
clickable data nodes shipped (#14), `WPSUBMIT_SITE_NOTICE` warns when a target cannot publish, and
the issue tracker was reconciled against the code (nine open at the time; five as of 2026-08-03).

**The app is deployed and live at https://upload.wikipathways.org**, pointed at the fork
`marvinm2/sandbox-wp-db` in `pipeline` mode. The handoff doc is authoritative for what is
proven, what is pending on other people, and the gotchas. Three things it says that matter most
here:

- **Approval does not merge.** On a target repo that publishes through its own Actions
  (`WPSUBMIT_PUBLISH_MODE=pipeline`), approving applies that repo's `accepted` label and stops;
  the repo assigns the WPID, publishes, and closes the pull request unmerged. A close without a
  merge is the *success* signal there — but only when the repo's marker comment says so. A silent
  close is `PUBLISH_FAILED`, for updates as much as for new pathways. `direct` mode still merges,
  and is the default, so `wikipathways-database`, a personal fork and the demo are unchanged.
- **A new pathway carries no WPID** until publication. It is submitted on branch
  `WP0001_<user>_<stamp>` at `pathways/WP0001/WP0001.gpml`; `Review.wpid` is nullable and the
  branch is recorded on the row, because it can no longer be derived. Revise is therefore keyed
  by pull request (`POST /api/reviews/{pr}/revise`), not by WPID. `WP0001` is a placeholder and
  **not** an address: the WPID routes refuse a leading zero rather than coercing it to WP1.
- **Every `ReviewStatus` is reachable in the UI.** `app/review/status.py` owns the on-screen
  vocabulary — the label, the banner sentence, the empty state, and which tabs the queue shows
  per publish mode. It also owns `ACTIONABLE` (open / changes_requested), which gates both the
  controls and `CurationService`'s own refusals. Add a status there, not in the template.

- **The fork now produces the target repo's own rendered draft page.** A fork inherits no Actions
  secrets, so `commit-outputs` had 403'd on every run the fork ever had and no draft was ever
  written — which is also why 3a died instantly, since it looks for a draft first. The fix is
  entirely account-side: `marvinm2/sandbox-wp.gh.io` and `marvinm2/sandbox-wp-assets` are forked,
  each with its own write-enabled deploy key (`ACTIONS_SANDBOX_DEPLOY_KEY` /
  `ACTIONS_SANDBOX_ASSETS_DEPLOY_KEY`), Pages is on with `baseurl: "/sandbox-wp.gh.io"`, and every
  `repository:` in workflows 1/3a/3b names a fork. The app needed **no code change** — just
  `WPSUBMIT_DRAFTS_REPO` and `WPSUBMIT_DRAFTS_SITE_BASE_URL`. Run `30451444585` is the first
  all-ten-jobs-green run of workflow 1 anywhere.
- **A pathway has been published — WP5423, run `30460071900`, every step green.** The first ever,
  on the fork. All three pushes landed (assets included), the marker comment carried the WPID, the
  pull request closed unmerged, and the app moved itself to `published` by reading that marker
  over the webhook. The drafts are *moved* at publication, so a draft page 404ing afterwards is
  correct. Approve was applied as a label directly rather than through the dashboard, because
  PR #5's checklist legitimately fails — a pass that *starts* at the Approve button is still
  outstanding.
- **Never merge a pipeline pull request** (2026-07-30, PR #11 on the fork). Merging commits
  `pathways/WP0001/WP0001.gpml` to `main`, and that is the placeholder slot every new submission
  writes to — the app created rather than updated it, so every submission by anyone then failed
  with `422 "sha" wasn't supplied` until the file was deleted by hand. Fixed in four layers
  (submission overwrites; the mirror comment says not to merge; the webhook deletes a stray
  placeholder off the base branch via `CurationService._repair_stray_placeholder`; a merged
  pipeline PR still settles from the publish marker rather than falling through to `MERGED`).
  `docs/sandbox-pipeline.md` §6 defect 12 has the full account. The general lesson is broader
  than this path: a **shared fixed path is never safe to create**, only to upsert.
- **Never put a GitHub expression in a `run:`-block comment.** A `run:` block is one string value
  and the runner substitutes into its *text* before bash exists, so `#` protects nothing; an
  expression that does not parse fails the **whole workflow at startup**, naming the `run:` line
  rather than the comment. It kept 3a from starting at all. `tests/test_sandbox_workflows.py`
  parses every staged workflow and rejects this.

`docs/sandbox-pipeline.md` maps the target repo's five workflows and its known breakages; §7 is
the fork-specific setup and is the one to read when a run goes red. `sandbox-workflows/` holds
repaired copies staged for a pull request to that repo — **not opened yet**, and not part of this
app; the fork already runs them.

## Open decisions (still unresolved — scaffolding-plan §0, proposal §9)

- Final repo name (`wikipathways-curator` vs `wikipathways-submit` vs `pathway-portal`).
- License (likely Apache-2.0 to match org software norm; content is CC0 but this is code).
- Curator whitelist mechanism: GitHub Team vs repo-tracked config file.
- Bot merge vs `main` branch protection interaction.
- Lock / reservation TTLs (tune against real submitter behaviour).
