# Scaffolding plan — `wikipathways-curator`

Companion to [`design-proposal.md`](design-proposal.md). This is the plan to stand up the new
repo: name, stack, layout, GitHub/identity setup, registry schema, CI/CD, and an initial issue
set mapped to the four MVPs. **No application code is written here yet** — this is the blueprint
to scaffold from.

---

## 0. Decisions to lock before `gh repo create`

| Decision | Recommendation | Notes |
|---|---|---|
| **Repo name** | `wikipathways-curator` | Covers submit + curate. Alt: `wikipathways-submit`, `pathway-portal`. |
| **License** | Match org convention for tooling (likely **Apache-2.0**) | Content is CC0; this is code, so follow the org's software norm. |
| **Backend language** | **Python** (FastAPI) | Marvin's primary language; async, OpenAPI docs, clean OAuth/webhook handling. |
| **Frontend** | Server-rendered templates + light JS for MVP; a small SPA only if the dashboard needs it | Avoid a heavy SPA until MVP-4 warrants it. |
| **Datastore** | **PostgreSQL** (SQLite acceptable for MVP-2 dev) | Holds WPID reservations, locks, curator config cache, review state. |
| **GitHub integration** | **GitHub App** (privileged bot) **+ per-user OAuth** | See §3 — two identities, deliberately. |
| **Hosting** | Strato cluster service, image via GHCR, Traefik-routed | Follow cluster conventions (GlusterFS data, `core` overlay, no node pinning). |

---

## 1. Repository layout (proposed)

```
wikipathways-curator/
├── README.md
├── LICENSE
├── CONTRIBUTING.md
├── .github/
│   ├── workflows/
│   │   ├── ci.yml                 # lint + test + build image → GHCR
│   │   └── deploy.yml             # (later) trigger cluster redeploy
│   └── ISSUE_TEMPLATE/
├── docs/
│   ├── design-proposal.md         # the "why" + architecture (already here)
│   ├── scaffolding-plan.md        # this file
│   └── architecture.md            # (later) diagrams, sequence flows, data model
├── app/                           # FastAPI application
│   ├── main.py
│   ├── auth/                      # GitHub OAuth (submitter) + GitHub App (bot) clients
│   ├── github/                    # PR create/update, branch push, merge, comment
│   ├── wpid/                      # atomic allocator (tree ∪ open PRs ∪ reservations)
│   ├── locks/                     # pathway check-out registry
│   ├── submit/                    # new-pathway + update flows
│   ├── review/                    # dashboard + approval state
│   └── models/                    # DB models (reservations, locks, reviews)
├── templates/                     # server-rendered UI
├── static/
├── migrations/                    # DB migrations (alembic)
├── tests/
├── Dockerfile
├── docker-compose.yml             # local dev (app + postgres)
├── pyproject.toml                 # deps (uv/ruff), tooling config
└── config.example.yaml            # curator whitelist, repo target, TTLs
```

And the **one file that ships into the content repo** (separate PR to
`wikipathways-database`, not this repo):

```
wikipathways-database/.github/workflows/pr-preview.yml   # render + validate on pull_request
```

---

## 2. Tech-stack rationale (short)

- **FastAPI + httpx + a GitHub client** (`githubkit` or `PyGithub`): OAuth callback, GitHub App
  JWT/installation tokens, and webhook receipt all fit cleanly.
- **PostgreSQL + SQLAlchemy + Alembic**: the registry must be transactional (atomic WPID
  reservation, lock acquisition). This is the one place we cannot be sloppy — see §4.
- **ruff + pytest + uv**: standard modern Python toolchain.
- **Docker → GHCR → cluster**: identical pattern to the existing VHP4Safety services, so both
  swarm nodes can pull and failover works.

---

## 3. Identity model — two GitHub identities, on purpose

1. **Per-user OAuth (submitter identity).** The submitter logs in with GitHub OAuth. The token
   is used to push the branch / open the PR **as them**, so authorship is real and attributed.
   Removes the need for the user to run git locally.
2. **GitHub App (bot identity).** A GitHub App installed on `wikipathways-database` performs
   **privileged, cross-cutting** actions the user token shouldn't: posting the preview comment,
   merging on curator approval, and (optionally) enforcing branch protection. It also gives the
   app a stable identity for webhooks (PR opened/closed, to expire locks).

> Setup tasks: register the GitHub App (permissions: contents RW, pull_requests RW, issues/
> comments RW, members read if using a Team for curators); create the OAuth App (or reuse the
> GitHub App's user-to-server flow); store secrets as Docker secrets on the cluster, never in
> the repo.

---

## 4. Registry schema sketch (the transactional core)

Two tables carry the invariants that today's workflow violates:

- **`wpid_reservation`** — `(wpid PK, reserved_by, reserved_at, pr_number NULL, status)`.
  Allocation is `INSERT` of `max+1` computed over **repo tree ∪ open PRs ∪ live reservations**
  inside one transaction, so two simultaneous new-pathway submissions cannot collide (the bug
  behind WP5637–5641). Unmerged reservations **expire** and return the ID to the pool.
- **`pathway_lock`** — `(wpid PK, held_by, acquired_at, expires_at, pr_number)`. Acquiring a
  lock is a conditional `INSERT`/upsert; check-out also **scans GitHub for an open PR touching
  that pathway** and refuses if one exists (power users can still bypass the app with a raw PR).
  Locks auto-expire and curators can force-release.

Plus a lightweight **`review`** table for dashboard approval state (the single source of truth
that the read-only PR comment mirrors).

---

## 5. The PR-preview workflow (MVP-1, ships to `wikipathways-database`)

- New file `.github/workflows/pr-preview.yml`, trigger `pull_request` on `pathways/**/*.gpml`.
- Reuses the existing render/metadata generators (subset of `on_gpml_change.yml`): produce
  rendered **SVG**, `-datanodes.tsv`, `-bibliography.tsv`, and a **validation report** (schema
  sanity, empty `<bp:ID>`→NA, missing identifiers, broken refs).
- Writes to a **preview artifact** and posts a summary **comment** — it does **not** commit
  derived files or push to sister repos.
- Delivers the biggest curation win with zero app and no new repo — can be built and merged
  first, in parallel with scaffolding this repo.

---

## 6. Initial issue set (maps to the four MVPs)

**Milestone: MVP-1 — Reviewable PRs (in `wikipathways-database`)**
- [ ] Add `pr-preview.yml` running render + validation on `pull_request`
- [ ] Factor the reusable render/metadata steps out of `on_gpml_change.yml`
- [ ] Post rendered SVG + datanode/reference tables + validation as a PR comment
- [ ] Define the validation checklist (rules + severities)

**Milestone: MVP-2 — Submission app, new pathways (this repo)**
- [ ] Scaffold FastAPI app, Dockerfile, CI → GHCR, cluster service
- [ ] GitHub OAuth login (submitter) + GitHub App registration (bot)
- [ ] Atomic WPID allocator over tree ∪ open PRs ∪ reservations (+ tests for the race)
- [ ] New-pathway flow: upload GPML → assign WPID → name/lay out `pathways/WP<id>/` → open PR
- [ ] Metadata capture form (description, organism, authors → `author_list.csv`, ontology tags)

**Milestone: MVP-3 — Updates + lock (this repo)**
- [ ] `pathway_lock` registry + acquire/release/expire logic
- [ ] Check-out flow with open-PR detection and curator force-release
- [ ] Update flow: branch off latest `main`, open/update PR

**Milestone: MVP-4 — Curation dashboard (this repo)**
- [ ] Reviewer dashboard: queue + before/after rendered view + data-node/reference tables
- [ ] Structured curation checklist + approve-that-merges (app owns approval state)
- [ ] Read-only PR-comment mirror synced to dashboard state
- [ ] Reviewer auto-assignment (by organism / community / prior authorship)
- [ ] Curator whitelist mechanism (GitHub Team vs repo-tracked config)

**Cross-cutting**
- [ ] `config.example.yaml` (target repo, curator list, lock/reservation TTLs)
- [ ] Docker secrets handling on the cluster
- [ ] Decide + document branch-protection interaction with bot merges

---

## 7. First concrete steps

1. Lock the **repo name** and **license**.
2. `gh repo create wikipathways/<name>` (public), push this `README` + `docs/`.
3. Open the milestones and issues from §6.
4. **Start MVP-1 immediately** as a normal PR to `wikipathways-database` — it needs none of
   this app and gives curators reviewable PRs the soonest.
5. Scaffold the FastAPI skeleton (empty `app/` structure from §1) so MVP-2 has a home.
