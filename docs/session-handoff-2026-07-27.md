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

Not yet exercised live: **approve**, **reject**, **publish detection**, and the **update** flow.

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

**Security, not fixed by us, worth raising with the maintainers.** `update-pr-desc` splices
`${{ needs.*.outputs.pr-desc }}` straight into a `run:` shell string. Those outputs are built from
the GPML's `Name`, `Organism` and description — fully submitter-controlled — in a job running
under `pull_request_target` with a write token and a checkout of the PR head. That is Actions
script injection, and anyone can open a pull request there. Written up in
`docs/sandbox-pipeline.md`.

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

## Next steps

1. **The update flow**, unexercised. `demo/Test_pathway_update.gpml` adds a glucose node and a
   third interaction. The route targets `pathways/WP<id>/WP<id>.gpml` on `main`, so it needs a
   WPID already in the fork's tree — `WP1001` or `WP554`. The content will not match that
   pathway, which is fine for the mechanism and odd semantically; decide which.
2. **Approve / reject / publish detection**, unexercised. Approving applies `accepted`, the
   dispatcher fires 3A, and 3A's marker comment is what the app reads back. Expect
   `PUBLISH_FAILED` on the fork, since 3A cannot push to the sister repos.
3. **Queue tabs for the new states.** Still only Open / Changes requested / Merged / Closed —
   Approved, Published, Publish failed and Rejected have no tab, so those reviews are unreachable
   from the queue.
4. **Draft artifacts in the UI** — `app/pipeline/drafts.py` and `refresh_pipeline_checks` are
   built and tested but have never run against real artifacts, because the fork cannot produce
   them. That needs the org install.
5. **Fork-per-submitter.** Right now the bot pushes the branch to the target repo, so the PR is
   authored by the bot. Real submitters have no push access to the org repo.
6. **Open the `sandbox-workflows/` pull request** once Marvin decides.

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
