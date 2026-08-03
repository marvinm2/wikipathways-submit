# Handoff — 2026-08-03

Supersedes `docs/session-handoff-2026-07-29.md` as the read-me-first. That file is still the
account of the deployment, the fork's draft pipeline and the first publication; this one covers
what changed after it. `docs/session-handoff-2026-07-27.md` remains the origin story of the
sandbox pipeline.

## Second round, same day — the audit issues (#17, #18, #21)

Three of the five open issues were closed after the quality-ruleset work below. All three came
from an audit rather than an incident, and all three are the kind that only bite once the target
is something other than a sandbox. 437 tests, ruff-clean. No migration; two new settings, both
left at their defaults on the live service.

**Deployed and verified live** at `sha256:45119303…` (built from `e7b7c8d`; superseded the same
day by `sha256:376eeee0…` from `cfdd938`, which added the timer work below). The sweep took the
live cache from 154K to 2.5K on the first dashboard load, and what it kept is the proof it works
rather than merely runs:

- **PR 1's render went, though its pull request is still open on GitHub.** The app's record says
  `rejected`, which is terminal; the pull request is open only because the fork's rejection
  workflow never closes one. The sweep keys off the *review*, not the pull request, and that is
  the difference showing up in practice.
- **PR 3's render was kept.** It is `PUBLISH_FAILED` — approved, never published — which is
  deliberately not terminal, because it is waiting on the person who most needs to see the
  diagram.
- All 24 drafts entries went and the reader refilled on the next load, which is the whole claim
  that deleting past the TTL is behaviour-neutral.

**#18, the render cache, was half-built and leaking on the path that matters.** `PreviewService.
discard` existed and was wired to every terminal transition a curator can reach through the
dashboard — and to neither of the two that happen without them. `_settle_publication` is the one
that counts: in pipeline mode it is *how a submission succeeds*, so the live deployment has been
leaking a directory on every pathway it publishes. Rejecting with the repository's own label
rather than the dashboard button leaked the other. Both are wired now, but the lesson is that a
list of call sites drifts, so the real fix is `PreviewService.sweep`, keyed off the state itself
and running off the dashboard reconcile on the set of non-terminal reviews that pass already has
in hand. A cache younger than an hour is never taken — a render is written *before* its review
row exists, so for that moment every new submission is indistinguishable from an orphan.

> **There is a second cache in the same directory, and it had the same defect.** `preview-cache/
> drafts` holds the `DraftsReader` entries; they expire by TTL and the file stays. The live
> volume is carrying a whole scope's worth from before the drafts repo was repointed at the fork,
> which nothing will ever read again. It has its own sweep now, on the same throttle. The render
> sweep's "ignore anything not named after a pull request" guard turns out to be what was already
> protecting that directory, which is less hypothetical than it looked when written.

**#17, the queue, now pages at twenty.** A card is not a row — two preview frames, a hotspot
sidecar per frame, the checklist, the data-node table, the quality panel, and a pipeline section
that can cost three requests to the drafts site on a cache miss. Two things only visible once
there is a second page: the order is tie-broken on the primary key (two submissions in the same
tick order arbitrarily on `created_at` alone, which becomes a row on both pages or neither), and
a page past the end lands on the last one rather than rendering empty.

> **The pager links are relative, and that is load-bearing.** Nothing in the deployment tells
> uvicorn to trust `X-Forwarded-Proto` — no `--proxy-headers`, and `forwarded_allow_ips` defaults
> to localhost while Traefik reaches the container over the overlay network. So `request.url`
> reports `http` on a site served over `https`, and an absolute link would point off the secure
> origin. Anything else built from `request.url` has the same problem waiting in it.

**#21, rate limiting, is ten pull requests an hour per account.** Counted out of the `review`
table rather than a bucket in memory, because the app is a single replica whose only shared store
is the database and an in-process counter resets on every redeploy. Keyed on the GitHub login,
not the address. Exempt: re-uploading onto a pull request that already exists, which opens nothing
and is how a submitter answers a change request. Not covered at all: `/api/validate`, which has no
login to key on and wants a blunt bound at Traefik if it ever needs one. `WPSUBMIT_SUBMIT_RATE_
LIMIT=0` disables it; see `docs/deployment.md`.

## The timers, set from measurement (#23)

#23 was filed saying its key number was unknowable "because it has never succeeded once". It has
since, so all three timers now have evidence behind them. Live at `sha256:376eeee0…`, 442 tests.

- **`publish_timeout_minutes` 30 → 10.** Label applied to pull request closed was **70s** (PR 5)
  and **42s** (PR 11) on the two publications that actually worked, with ~10s of queueing. Ten
  rather than two or three because the failure directions are not symmetric: declaring failure
  late costs a curator some waiting, but declaring it early puts "never published" on screen under
  a publication that is merely queued — and the documented response to that is re-applying the
  `accepted` label, which dispatches a **second** publish run.
- **`pathway_lock_ttl_days` 3 → 14.** A lock is held for the life of the pull request, so the
  evidence is the 53 closed pull requests on `wikipathways/wikipathways-database`: median 0.36
  days, 90th 6.8, 95th 7.8. The 3-day TTL expired under **26% of real reviews**. Being precise
  about the cost: an expired lock does not hand the pathway over outright, because a fresh
  check-out re-runs the open-PR scanner — but it downgrades the guarantee from a database
  constraint to a best-effort GitHub read that **fails open**.
- **`wpid_reservation_ttl_days` unchanged at 14**, which already covered 96%.

The interim the issue asked for is built too, since these will want correcting again once real
submitters arrive: every lock expiry and reclaimed reservation logs how long it was held, by whom
and against which pull request, and `PathwayLockRegistry.age_of` gives the dashboard something to
show. The quiet pass stays silent, because `expire_stale` runs on every acquire.

> [!warning] Alembic's `fileConfig` disables every logger it does not name
> Found because those tests passed alone and failed in a full run. `migrations/env.py` called
> `fileConfig(...)` with alembic's default `disable_existing_loggers=True`, which switched off
> every `wpsubmit.*` logger for the rest of the suite. Production never noticed — the entrypoint
> runs `alembic upgrade head` as its own process before uvicorn starts — but the default is wrong
> for any in-process caller, and it is now `False`. Worth knowing for any project that runs
> migrations programmatically.

## A GPML with no declared encoding converts to nothing

Third round, same day. Found by looking at why workflow 1 was red on the fork: the last three
**new-pathway** runs (`30805539734`, `30800359486`, `30798868327`) all failed while the update
between them went green. The failing job is `json-svg`, and the error is a Node stack trace —
`SyntaxError: Unexpected end of JSON input` out of `add_identifiers`.

**`gpml2pvjson` returns zero bytes and exit status 0** for a GPML whose XML declaration omits
`encoding`, or that has none at all. Exit 0 is what makes it nasty: `set -e` cannot catch it, so
the failure surfaces one step later in something that looks unrelated.

Reproduced locally against gpml2pvjson 4.1.8 from both directions, which is what makes it a
finding rather than a guess — and it is independent of pathway content:

| file | bytes out |
|---|---|
| PR 15's GPML as committed (`<?xml version="1.0"?>`) | **0** |
| the same file with `encoding="UTF-8"` added | 1915 |
| a submission that converted fine, untouched | 3780 |
| that same file with `encoding` stripped | **0** |
| that same file with the declaration removed entirely | **0** |

The app was passing the declaration through verbatim, so it was committing files **its own
renderer draws quite happily** and the pipeline cannot read — the third instance of that pattern
after the missing root `<Graphics>` canvas, and worth treating as the house failure mode by now.

Fixed in `app/submit/gpml.py`: `assign_wpid_str` now writes `<?xml version="1.0"
encoding="UTF-8"?>`, replacing or inserting as needed. It is the one choke point all three write
paths go through. Rewriting rather than warning is the honest move — the app already decodes
every upload as UTF-8 and commits UTF-8 bytes, so a file that arrived declaring something else is
mislabelled by the time it is written either way; this makes the declaration match the bytes.

> No quality rule was added for it. The ruleset predicts what the target pipeline will do, and
> after this change the answer is "it converts" — a rule warning otherwise would be wrong.

## Where #22 (fork-per-submitter) actually stands

Assessed, not built — it is a design decision. The full write-up is on the issue; the three things
that change the picture:

- **Fork pull requests are already the norm on the target.** 36 of the 53 closed pull requests on
  `wikipathways-database` came from contributor forks, against 17 from the repo itself. So this is
  not a new mode to introduce; the app's bot-pushes-a-branch model is the unusual one there.
- **The OAuth scope cost is very likely not real.** The app already requests `public_repo`, which
  GitHub defines as read/write to code on public repositories, and both targets are public. Worth
  a five-minute empirical check before relying on it.
- **The real blocker is not in the issue's list.** `find_open_pr` hard-wires
  `head={base_owner}:{branch}`, so for a fork pull request GitHub returns nothing and **revise
  breaks entirely** — a curator requesting changes would leave the submitter unable to answer.
  Two more places treat a branch name as a globally unique identity for an edit, and branch names
  are only unique within one repo.

The decision underneath it: `WPSUBMIT_SUBMIT_IDENTITY=bot` exists because submitters cannot push
to the target, and a bot installation token cannot push to a submitter's personal fork either — so
fork mode forces the user token back into the write path, which is what `bot` mode was introduced
to avoid.

### Step 3 of that order is now built, and it turns no fork mode on

The issue's suggested order marks step 3 "worth doing regardless", because those are latent
correctness bugs the moment anything cross-repository appears — including a raw fork pull request
opened by a power user today, which is how most work already reaches that repository. Built:

- **`find_open_pr` takes the head repo.** It defaults to the base repo, so every existing caller
  is unchanged. `PullRequest` now carries `head_repo` too, read off GitHub's answer rather than
  assumed, so the day the app opens a cross-repository pull request the review row records the
  truth on its own with no further change.
- **The lock's open-PR scanner only treats a same-repo head as "one of ours".** It was skipping
  any ref starting `submit/WP<id>` or `update/WP<id>` *before* the file check, so a fork branch of
  that name would be skipped and the check-out granted over somebody's genuine concurrent edit.
  Narrowing it fails the safe way: at worst one file listing and a lock refused that could have
  been granted.
- **`Review.head_repo`**, nullable, NULL meaning the content repo — which is every row so far, so
  no backfill. Migration `f6a2c3e4d5b7`. Storing `owner:branch` in `head_branch` instead would
  have poisoned revise silently, which is why it is a separate column.
- **Revise scopes its branch-side reads and writes to the head repo**, so a fork pull request
  revises onto the fork where its branch actually is. The base repo is still what gets queried
  for the pull request; only the head moves.

Twelve tests in `tests/test_cross_repo_head.py`, half against the real client through
`httpx.MockTransport` (the head filter GitHub actually receives, the `head.repo.full_name` parse,
the deleted-fork null) and half against the fake.

**Still not decided, and still the real question:** whether the user OAuth token goes back into
the write path. Steps 1 and 2 both need a person — a second GitHub account to prove `public_repo`
really forks and pushes, and a fork pull request published end to end on the sandbox.

One thing that did change in step 2's favour: **the label dispatcher has recovered.** The issue
records it failing 5 of its last 6 runs on fork pull requests; every `PR Label Dispatcher` run in
the current window is green. Whatever was wrong there is not wrong now.

Still open after this round: **#22** only.

## Deployed right now

**https://upload.wikipathways.org**, image
`ghcr.io/marvinm2/wikipathways-submit@sha256:376eeee07392c889dd873dbba8b7bf3cd3e889efe4d7d4171b68804f29b300dc`
(built from `cfdd938`), running on **tgx1**. 442 tests, ruff-clean.

The previous digest was
`sha256:8d8dda3e3e115aab0418245a05b358023222667253d167a0b887332b57a6e658` (from `0db9c1b`), which
is the rollback target. Neither round added a migration, a secret or a required env var, so a
rollback in either direction is a plain digest change.

Deploy by digest, never by `:latest` — a bare tag is a no-op on swarm. And find the node before
reading logs; an empty log on the wrong node is indistinguishable from an app that never got the
request.

## What was built: one graded quality ruleset

Quality control had grown into five layers in four vocabularies, and the richest of them had never
run on a real submission. `app/quality/` is now the single ruleset. Read its module docstring
first — it says where every rule came from and why.

- The four blocking reasons keep their **exact wording**. `InvalidGpml.reasons` is read into
  `detail.errors` and rendered to submitters by `describeError` in `static/app.js`, so those
  strings are interface, not prose. `validate_gpml` is now *defined as* the `block` subset, which
  is what stops the portal refusing a file for a reason its own report called fine.
- Severities are `na < pass < warn < fail < block`. `na` ranks **below** pass deliberately, so
  "nothing to check" can never win a rollup and report an empty pathway as all-green.
- **The package must import nothing from `app.*` at module scope.** `app.models` imports
  `app.review.checklist`, which imports this, and `app.submit` reaches `app.models` through the
  allocator. An eager import closes that cycle and fails at *startup*, not in a test. Metadata is
  duck-typed and the one call inward is function-local; `test_quality_imports_no_app_package`
  walks the AST and fails if anyone forgets.

Three surfaces, one report: `/api/validate` (which nothing called before — the submit form now
posts to it on file choice), one "Automated checks" block on the review card that absorbed the four
scattered signals, and a table in the mirror comment.

**`_render_preview` runs before `register` in all three write paths.** `register` posts the mirror
comment and reads the report out of the render cache, so the other order left the table missing
from the first comment on every pull request — on the one artifact a GitHub-native reviewer sees.

## The checklist now matches the repository's own

Workflow 1 appends a seven-item reviewer checklist to every pull request body; the app had its own
six, overlapping on four. Added `interactions_connected`; gave `description_ok` an auto-check.

**That auto-check can never return `pass`, on purpose.** `refresh_pipeline_checks` only writes
items still `pending`, so anything the app puts there pre-empts the repository's own description
check — and that one quotes the text its extractor actually pulled out, which is what reaches the
published page. The binding maps both `pass` and `warn` to `pending`; `references_valid` has the
same shape for the same reason.

## The two systems can now see each other

`marvinm2/sandbox-wp-db`'s workflow 1 posts
`<!-- wikipathways-testing {"pr":N,"title":…,"description":…,"nodes":…} -->` as a **comment** — the
device 3a's publish marker already uses, and for the same reason: `update-pr-desc` rewrites the
pull request description wholesale on every run, so a description is not somewhere anything can
publish to. `parse_testing_marker` reads it into `Review.pipeline_run["testing"]` on reconcile, and
the card shows the repository's verdict beside the portal's prediction of the same check.

Proven 2026-08-03 on two pull requests: #15 round-tripped three `review`, #16 two `pass` and one
`review`, so both verdict values work.

> **A disagreement is a signal, not a bug.** On #16 the portal said the description was missing
> (`fail`) and the repository said `pass` — because the repository's description test measures
> *change* on an edit and structurally cannot see an absence. Read a mismatch as "these two are
> measuring different things" first, and only suspect a drifted threshold once that is ruled out.

**The marker step is on the fork only.** It has not been offered to
`wikipathways/sandbox-wp-db`, so a deployment pointed upstream shows no repository verdicts and
degrades quietly, as designed. The staged copies live in `sandbox-workflows/` — read the warning at
the top of its README before applying them anywhere.

## Two findings that cost real time, and generalise

**A GPML with no root `<Graphics>` canvas kills the repository's `metadata` job.** Measured with
two submissions of one pathway differing in that single element: run `30798868327` (absent) failed
with `ConverterException: NullPointerException` out of `GPML2013aReader.readPathway`; run
`30800359486` (present) succeeded. `metadata` is what the whole downstream fan-out needs, so the
submitter loses their identifier table, bibliography and draft page for a Java stack trace several
clicks into the Actions tab. Now `gpml.board`, severity `fail`.

It survived this long because **the portal's own renderer draws such a file quite happily** — and
because the test fixtures had no canvas either. Two readers of the same format disagreeing, with
only one of them on the path to publication, and fixtures less demanding than the real pipeline.
Both halves of that are worth watching for elsewhere.

**Adding a panel to fix a scrolling problem made it worse before it made it better.** The first
version of the Automated checks block spelled out every finding on the queue and pushed the action
bar *further* down than it had been. It is a summary line there now ("2 problems, 3 worth a look")
and full on the detail page. Net movement of the action bar: 1699px → 1207px down the card.

## Gotchas, cumulative

Everything in the 07-29 list still holds. Added this round:

- **Deploy by digest.** `docker service update --image …:latest` is a no-op; pull first, read
  `RepoDigests`, update to the `@sha256:` form.
- **`gh run list --limit 1` races the push.** Query by `headSha`, or watch by id.
- A 404 from `/health` seconds after `docker service update` converges is the rollout, not a
  failure. Re-check before diagnosing.
- **`document.cookie` is refused in the browser profile**, so a logged-in page cannot be reached
  by setting a cookie from JS. Fetch the HTML with `curl -b`, drop it beside a copy of `static/`,
  and serve that. Screenshots of such a snapshot page are flaky; measure with `javascript_tool`
  instead of trusting a screenshot.
- **The harness permission classifier refuses writes to a repository other than this one** — both
  `gh api -X PUT` and `git push`. That is the right call; it waited for Marvin, who ran the push.
  The same guard has bitten twice before (a content delete, a production `UPDATE`), and each time
  asking was the answer rather than routing around it.
- When applying `sandbox-workflows/` to a **fork**, apply the *changes*, never the files. The
  staged copies name the upstream repositories in their `repository:` inputs; a fork's own copies
  name the fork, and a wholesale copy repoints its draft push at a repo it cannot write to. The
  failure is silent, because a missing draft is the ordinary case.

## Still needing a person

Everything the 07-29 handoff lists under this heading is still open — the security disclosure, the
GitHub App install on the org sandbox, the curator list being Marvin only, and no notification to a
submitter when changes are requested. Added:

- **The `note_test_nodes` / `review_note` text still uses a literal `\n`**, so the PR table reads
  `Modified nodes: 3\n`. Same class of bug as the counts that were fixed, but in the prose rather
  than the arithmetic, and nothing reads it. A one-line fix whenever that job is next touched.
- **Nothing has been offered upstream.** `sandbox-workflows/` is staged and now diverges from the
  fork only in the `repository:` lines.
- **Login as a genuinely different GitHub account** remains untested, as it has since 07-29.

## Live check commands

```bash
# GET, not HEAD: the route is GET-only, so `curl -sI` answers 405 and reads like an outage.
curl -s https://upload.wikipathways.org/health
curl -s -X POST https://upload.wikipathways.org/api/validate -F file=@some.gpml \
  | jq '.quality.findings[] | select(.severity != "pass" and .severity != "na")'
ssh tgx1 "docker service ps wikipathways-submit -f desired-state=running --format '{{.Node}}'"
ssh tgx1 "docker service inspect wikipathways-submit --format '{{.Spec.TaskTemplate.ContainerSpec.Image}}'"
gh api repos/marvinm2/sandbox-wp-db/issues/<pr>/comments --jq '.[].body' | grep wikipathways-testing
```
