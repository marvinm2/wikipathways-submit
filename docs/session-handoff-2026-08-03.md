# Handoff — 2026-08-03

Supersedes `docs/session-handoff-2026-07-29.md` as the read-me-first. That file is still the
account of the deployment, the fork's draft pipeline and the first publication; this one covers
what changed after it. `docs/session-handoff-2026-07-27.md` remains the origin story of the
sandbox pipeline.

## Deployed right now

**https://upload.wikipathways.org**, image
`ghcr.io/marvinm2/wikipathways-submit@sha256:8d8dda3e3e115aab0418245a05b358023222667253d167a0b887332b57a6e658`
(built from `0db9c1b`), running on **tgx1**. 401 tests, ruff-clean. No new env var, no new secret
and no migration in this round — the quality report is cached in the render cache on `/data`, so a
rollback is a plain digest change.

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
curl -sI https://upload.wikipathways.org/health
curl -s -X POST https://upload.wikipathways.org/api/validate -F file=@some.gpml \
  | jq '.quality.findings[] | select(.severity != "pass" and .severity != "na")'
ssh tgx1 "docker service ps wikipathways-submit -f desired-state=running --format '{{.Node}}'"
ssh tgx1 "docker service inspect wikipathways-submit --format '{{.Spec.TaskTemplate.ContainerSpec.Image}}'"
gh api repos/marvinm2/sandbox-wp-db/issues/<pr>/comments --jq '.[].body' | grep wikipathways-testing
```
