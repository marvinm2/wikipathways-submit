# Handoff — 2026-07-27

Where the sandbox integration and the first deployment got to, what is proven against live
GitHub, and what is still open. Read this before picking the work back up.

## Deployed

**https://upload.wikipathways.org** — live, Let's Encrypt certificate, HSTS on.

Two swarm services on the Strato cluster (`services/wikipathways-submit.md` on tgx1 has the
full record, and the service-registry row is added):

| Service | Image | Notes |
|---|---|---|
| `wikipathways-submit` | `ghcr.io/marvinm2/wikipathways-submit` | scheduled on **tgx2**, not pinned |
| `wikipathways-submit-db` | `postgres:16` | on tgx1, GlusterFS-backed, `stop-first` |

All three Alembic revisions applied against the real Postgres on first boot.

### Secrets (all created)

`wpsubmit_db_password`, `wpsubmit_database_url`, `wpsubmit_session_secret`,
`wpsubmit_token_encryption_key`, `wpsubmit_oauth_client_secret`, `wpsubmit_app_key`.

### Live configuration

```
WPSUBMIT_CONTENT_REPO=marvinm2/sandbox-wp-db     <-- the FORK, not the org repo
WPSUBMIT_PUBLISH_MODE=pipeline
WPSUBMIT_SUBMIT_IDENTITY=bot
WPSUBMIT_REQUIRE_PREVIEW_CHECK=false
WPSUBMIT_APP_BASE_URL=https://upload.wikipathways.org
WPSUBMIT_GITHUB_APP_ID=4403728
WPSUBMIT_GITHUB_APP_INSTALLATION_ID=149294202
WPSUBMIT_CURATORS=["marvinm2"]
```

`WPSUBMIT_REQUIRE_PREVIEW_CHECK=false` is load-bearing. It defaults to true and gates on
`pr-preview.yml`, which does not exist on the sandbox — left at the default, every approval
returns 409.

## Waiting on other people

- **The GitHub App install on `wikipathways/sandbox-wp-db`** is requested and pending an org
  owner. Marvin is a *member*, not an owner, so he can only request. The dev App is installed on
  his own fork, which is why the fork works: a private App can always be installed on the account
  that owns it.
- **The `sandbox-wp-assets` write credential** — nobody in reach has it.
- **Rotate the GitHub App client secret.** It was pasted in plain text during the session and
  written to a file on tgx1 (since shredded). Nothing we run uses it — the bot authenticates with
  the private key — so deleting it outright is the clean fix.

## Proven against live GitHub

Submission, change request and revision, all through the browser at
`https://upload.wikipathways.org` against `marvinm2/sandbox-wp-db` PR #2:

- branch `WP0001_marvinm2_<stamp>`, file at `pathways/WP0001/WP0001.gpml`
- PR opened by the bot; **commit authored by the submitter** and linked to their GitHub account
- `new pathway submission` label applied, mirror comment posted
- their workflow 1 classified it as **New** and renamed it `WP0__PR2.gpml` — the assumption the
  whole placeholder scheme rests on, confirmed against the real workflow rather than a
  transcribed regex
- request changes → `changes_requested` + a comment on the PR
- revision uploaded in the browser → committed onto the **same** branch, no second PR, review
  back to `open`, and `datanodes_mapped` re-derived from the new content: FAIL → PASS

Not yet exercised live *at the time of writing*: **approve**, **reject**, **publish detection**,
and the **update** flow. All four were exercised on 2026-07-28 — see the section at the end.

## Upstream bugs found (all in `wikipathways/sandbox-wp-db`)

Fixed on the fork and staged in `sandbox-workflows/` for a pull request to the org. Not opened
yet — that is a decision for Marvin.

**Workflow 1, the first-contributor path.** Three defects in a row, each only reachable once the
previous was fixed. All three fire when adding an author who is not yet in `author_list.csv`,
which is a person's *first ever submission* — so every established contributor's submission works
and every newcomer's fails. That population is exactly who this portal exists to serve.

1. line 483 `$k=$k + 1` — PHP syntax; bash runs `0=0`, exit 127
2. line 1071 `cp author_list.csv` — the file is at `authors/author_list.csv` after the artifact
   round trip
3. line 1073 destination `scripts/` does not exist in `sandbox-wp.gh.io` (it has `_authors/`,
   `_drafts/`, `_data/drafts/`, `draft_assets/` and no `scripts/`)

**3A publish workflow** (read out of the YAML, not observed — it has run once ever, 19 seconds,
failed, logs expired): `gh pr edit --add-body` is not a real flag; the WPID `sed` runs on a full
path and silently mis-assigns rather than aborting, because a failing `[` inside an `if` is exempt
from errexit; two dead `::set-output` calls; cross-repo checkouts with `GITHUB_TOKEN`; no
`permissions:` block; no `git pull --rebase`.

**Label dispatcher.** Not a missing `permissions:` block, which was the first guess. Its surviving
log shows a read-only token and a 403, and both failed runs came from forks while both successes
came from inside the repository — that is GitHub's fork cap on `pull_request`, which `permissions:`
cannot raise. 11 of the last 15 PRs there are from forks. Fixed by moving to
`pull_request_target`, which is why workflow 1 already uses it.

**A security defect, not fixed by us.** Workflow 1 has one, reachable by anyone who can open a
pull request against that repo. The mechanism is deliberately not described here or anywhere else
in this public repository — see `docs/sandbox-pipeline.md` §6.1 for why, and
`../sandbox-wp-db-disclosure-DRAFT.md` (outside the repo) for the analysis and the fix. Reported
to the maintainers privately.

## The fork, and what it cannot show

`marvinm2/sandbox-wp-db` has all 8 workflows registered and active, the full label vocabulary
replicated (forks do not copy custom labels), and **default workflow permissions set to write** —
GitHub gives new repos read-only, which made `gh pr edit` fail with "Resource not accessible by
integration" on the first run.

`commit-outputs` can never pass on the fork: it pushes to `wikipathways/sandbox-wp.gh.io` with a
deploy key the fork does not have, so it ends at
`Permission to wikipathways/sandbox-wp.gh.io.git denied to github-actions[bot]`. Nine of ten jobs
green is the ceiling here. No draft artifacts means the checklist pre-fill and the draft-page link
stay on their degraded path.

The bridge cache is warm (`cached-bridge-files`, 1.6GB), which cut a run from ~40 minutes to
about 3.

## What changed after this was first written (later on 2026-07-27)

A second pass closed items 3 and 4 below and a batch of defects an audit turned up. The state
machine and the dashboard now cover the whole pipeline lifecycle rather than the first half of it.

**Reachable states.** The queue has a tab per `ReviewStatus`, mode-dependent (`app/review/status.py`
owns the vocabulary): pipeline mode shows Open / Changes requested / Approved / Published / Not
published / Rejected / Closed and no Merged, because nothing merges there. Each tab carries a
count, each status has a badge, a banner and its own empty-state copy, and the banner renders
`decision_note` — until now the only carrier of "the accepted label has been on for 41 minutes and
nothing published" was a database column nothing displayed.

**Actions that were implemented but unreachable.** Reject (with a reason, and a warning that the
repository's workflow deletes the drafts and closes the pull request) and record-the-published-WPID
now have controls. Approve, Reject and Request changes only render on `open` and
`changes_requested`, and `CurationService` refuses them elsewhere with a 409 rather than trusting
the template.

**The repository's own output** — the draft page, its render, the data-node and reference counts,
the run link — is rendered in a panel. `_pipeline_view` was computing all of it and the templates
were showing one field of it.

**A submitter can find their own work.** `/dashboard?mine=1`, every status at once, because in
pipeline mode there is no WPID to look a submission up by. The revise upload is theirs, not only a
curator's.

**Publish state-machine fixes**, all of which were silent:

- a `publish_failed` review was rewritten to `closed` by the next reconcile, which is terminal —
  the pathway was stranded with nobody looking. `handle_pr_closed` now routes both `approved` and
  `publish_failed` through `_settle_publication`, so a late publication is still recorded.
- an approved *update* was recorded as published whatever happened, because the fallback took the
  WPID it already had and the "is it on main?" check passes trivially for a file that was there
  first. Publication now needs the marker comment.
- re-approving after a failed publication did nothing: GitHub emits no `labeled` event for a label
  that is already on the pull request. Approval now removes `accepted` before adding it.
- rejecting or requesting changes on an approved review left `accepted` in place, so the pull
  request carried both labels.
- rejecting by label on GitHub, and recording a WPID by hand, both leaked the pathway lock for the
  full TTL.

**Other defects the audit confirmed:** the update tab's revise path POSTed to
`/api/pathways/{wpid}/revise`, a route that stopped existing when revise was re-keyed by pull
request; `WP0001` typed into the update field was coerced to the integer 1 and addressed WP1, a
real unrelated pathway (route parsing now refuses a leading zero); the submit result card said
"Assigned WP0001"; the pathway lock was built without its open-PR scanner, so a raw pull request
opened outside the app was invisible to it; `getattr` on a dict made the pipeline's reference
check dead code; the second edit of a pathway reused a months-old branch while the PR body claimed
it was cut from latest; a re-uploaded update kept the checklist derived from the file it replaced;
and `naming_ok` auto-passed with "assigned by the app", which is false here and blocked the
repository's own naming check from ever being applied.

Plus the standing UI backlog from `docs/ui-review-2026-07-27.md` — items 9, 10, 13, 14, 15, 16,
17, 19, 21, 22, 24, 25, 26, 27, 29, 31, 32, 33, 34 and 35.

## Next steps

1. **The update flow**, unexercised. `demo/Test_pathway_update.gpml` adds a glucose node and a
   third interaction. The route targets `pathways/WP<id>/WP<id>.gpml` on `main`, so it needs a
   WPID already in the fork's tree — `WP1001` or `WP554`. The content will not match that
   pathway, which is fine for the mechanism and odd semantically; decide which.
2. **Approve / reject / publish detection**, unexercised against live GitHub. All of it is now
   covered against the fake, end to end. On the fork expect `PUBLISH_FAILED`, since 3A cannot push
   to the sister repos — which is now a first-class state with a tab and a way out.
3. **Draft artifacts** still have not run against real ones: the fork cannot produce them, so the
   panel that renders them has only been exercised against a stub. That needs the org install.
4. **Fork-per-submitter.** Right now the bot pushes the branch to the target repo, so the PR is
   authored by the bot. Real submitters have no push access to the org repo.
5. **Open the `sandbox-workflows/` pull request** once Marvin decides — keeping the workflow-1
   security defect out of it, since that one goes to the maintainers privately
   (`docs/sandbox-pipeline.md` §6.1).
6. **Lock and reservation TTLs** still want tuning against real submitter behaviour.

## Gotchas that cost time

- **`docker service update --image ...:latest` is a no-op.** The spec holds a bare tag, so Swarm
  sees no change and does not redeploy. Update by digest, or use `--force`.
- **Do not poll `gh run list --limit 1` right after pushing** — it returns the *previous*
  completed run and a wait loop exits immediately, deploying the old image. Key on the pushed
  commit's SHA.
- **Setting `WPSUBMIT_GITHUB_APP_PRIVATE_KEY_PATH` before the secret exists** used to crash-loop
  the service. Fixed: a configured-but-unreadable key now logs and disables the bot.
- **`gh api -X PUT ... -f content=<base64>` silently fails on a large file** — the argument is too
  long. Use `--input <body.json>`. Piping through `head` hides the non-zero exit.
- **An imported Jinja macro sees none of the calling template's context** without
  `{% from ... import x with context %}`. This made the repo name vanish from the failure notice
  on the review page while working fine on the queue page.
- **Browser automation:** `ref`-based clicks on the review page repeatedly reported success
  without doing anything. Coordinate clicks worked.

## Live check commands

```bash
curl -sI https://upload.wikipathways.org/health
ssh tgx1 "docker service ps wikipathways-submit"
ssh tgx2 "docker logs \$(docker ps -q -f 'name=wikipathways-submit.1') --since 5m"
gh run list -R marvinm2/sandbox-wp-db --limit 5
```

## Addendum — 2026-07-28: the rest of the lifecycle, live

The seven pending commits were deployed and the whole app driven through the browser against
`marvinm2/sandbox-wp-db`. This closes the "not yet exercised live" line above.

### Now proven against live GitHub

- **Approve** (PR #3) — applies `accepted`, upserts the mirror comment, posts the handover note.
  The button is genuinely `disabled` until every required checklist item passes and enables the
  moment they do. Worth knowing when testing: a disabled approve button and a broken one look
  identical from the outside.
- **Publish detection** — 3A failed without closing the PR, so no close event was ever coming.
  After the 30-minute `publish_timeout` the review moved `approved` → `publish_failed` on its own,
  shown as **Not published** with `decision_note` rendered on screen, actions restored (it is
  `DECIDABLE`), and the manual WPID control offered.
- **Record-the-published-WPID**, **reject** (PR #1, reason posted, controls correctly gone), and
  the **update flow** (PR #4): branch `update/WP1001`, head commit's parent byte-identical to the
  current `main` HEAD, so branch-off-latest is real rather than asserted. Re-upload reused the
  open PR.
- **The two-identity split**, visible on GitHub: commits authored by the submitter, PRs opened by
  the bot.

### The fork's ceiling is worse than recorded above

**3B (rejection) fails exactly as 3A (publish) does** — both push to
`wikipathways/sandbox-wp.gh.io`, for which the fork holds no deploy key
(`403 denied to github-actions[bot]`). So on this target **no pull request can ever close**,
whatever the app decided. When working here, read the app's state, not the pull request's. All
six PRs on the fork are OPEN regardless of being approved, rejected or published.

### Two defects found, fixed, deployed

- **The connection pool had no liveness check.** A pooled connection outlives its request and the
  overlay network drops idle TCP sessions silently, so the *first* request after any quiet period
  returned a 500 (`server closed the connection unexpectedly`) — exactly when a curator comes
  back. Self-heals on retry, so the symptom is one 500 per idle period, which is easy to misread.
  Fixed with `pool_pre_ping` + `pool_recycle` in `app/db.py`, non-SQLite only. **Unit-tested but
  not yet confirmed against a real idle window**; to confirm, grep the service log for
  `OperationalError` after a genuine quiet spell.
- **The mirror comment announced every update as "A edit".** Fixed; the noun carries its article.

### The portal has users who are not us

PRs #5 and #6 arrived through `upload.wikipathways.org` mid-session with nobody in the session
making them — #6 a **PPAR signaling pathway from @MadhushriMSV**, 88 data nodes, 14 references,
which the app parsed, rendered and checklisted correctly.

**#6 was a colleague testing the portal**, confirmed by Marvin the same day, and has been closed.
It was first read here as a contributor's real submission going into a dead end, which was a
guess presented as a finding: a pathway from an unfamiliar account is not evidence of intent, and
one question settled it. Nobody has lost work to this deployment.

Two things survive that correction. The queue is **shared**, so test submissions want labelling
as such and only ever approving or rejecting against your own. And from inside the app a test was
indistinguishable from the real thing — which is the actual case for `WPSUBMIT_SITE_NOTICE`
(`docs/deployment.md`): by the time you can tell them apart, a silent failure has already
happened. **Set it on any deployment whose target cannot complete a publication.**

### Still open

The org install on `wikipathways/sandbox-wp-db` (blocks draft artifacts and any real
publication), fork-per-submitter, TTL tuning, and the private disclosure of the workflow-1
security defect.

On that last one: §6.1 of `docs/sandbox-pipeline.md` used to describe it in full — mechanism,
line numbers, a working payload — in **this repository, which is public**. That is the exact
thing the decision to keep it out of the `sandbox-workflows/` pull request was meant to avoid,
and it sat there for a day. It has been redacted, and the analysis now lives outside the repo in
`../sandbox-wp-db-disclosure-DRAFT.md`. Redaction is not erasure: the repository has been public
and pushed since 2026-07-27, so assume it is mirrored and treat the timeline accordingly. The
lesson generalises — deciding not to publish something has to cover the notes as well as the
pull request.
