# sandbox-workflows

Repaired copies of two GitHub Actions workflows belonging to
[`wikipathways/sandbox-wp-db`](https://github.com/wikipathways/sandbox-wp-db). They are
staged here so they can be reviewed and then opened as a pull request against that
repository.

**These files ship into `sandbox-wp-db`, not into this app.** Nothing here runs as part of
the submission app, and nothing here is imported by `app/`. The directory mirrors the
target layout (`.github/workflows/...`) so the files can be copied across verbatim.

The three files:

- `.github/workflows/1_on_pull_request.yml` — the PR processor. Two one-line fixes on the
  new-contributor path; see "Workflow 1: the first-contributor path" below.
- `.github/workflows/3a_approved_pull_request.yml` — the publish workflow. Renames the
  draft files produced by workflow 1 to their final WPID, pushes them to `sandbox-wp-db`,
  `sandbox-wp.gh.io` and `sandbox-wp-assets`, announces the WPID on the PR, and closes it.
- `.github/workflows/pr_label_dispatcher.yml` — turns the `accepted` / `rejected` /
  `resubmitted` labels into runs of 3A / 3B / workflow 1.

`labels.md` lists the two labels that have to be created before 3A can use them.

## Workflow 1: the first-contributor path

Unlike the 3A changes, these two are **not** read out of the YAML — both were hit on a live
run, fixed, and the fix confirmed by re-running. They sit on the branch that adds an author
who is not yet in `author_list.csv`, so they fire on a person's **first ever submission**
and not otherwise. That is why they have survived: the recent test submissions all come from
contributors already in the CSV.

The consequence is worth stating plainly, because it is the opposite of harmless. When
`authors` fails, `commit-outputs` and `update-pr-desc` are skipped, so a first-time
contributor gets no draft page, no data-node table, no bibliography and no report on their
pull request — while everyone already in the CSV gets all of it.

**1. `authors`, line 483.** The counter used PHP syntax:

```bash
$k=$k + 1        # $k expands to 0, bash runs "0=0" as a command, exit 127
k=$((k + 1))     # fixed
```

Observed on `marvinm2/sandbox-wp-db` PR #2: `Adding marvinm2` followed by
`line 58: 0=0: command not found` and exit code 127.

**2. `commit-outputs`, line 1071.** With the counter fixed, the next run reached a second
defect on the same path:

```bash
cp author_list.csv wikipathways.github.io/scripts/.          # cannot stat
cp authors/author_list.csv wikipathways.github.io/scripts/.  # fixed
```

`authors` moves `scripts/author_list.csv` into `authors/` (line 487) and uploads that
directory as the `authors` artifact, so on download the file is at `authors/author_list.csv`.
The copy looked for it in the workspace root.

## What is known, and what is not

Read this section as the reason for the changes, not as a diagnosis. Nothing has ever been
published by 3A, and the evidence that would explain why is gone.

**Observed, from the API on 2026-07-27:**

- 3A has run exactly once. Run `17442557461` was a `workflow_dispatch` by `egonw` off
  `main` on 2025-09-03, ran from 18:25:34Z to 18:25:53Z — **19 seconds** — and failed.
- That run's logs are expired (HTTP 410) and its steps array is empty, so **which step
  failed cannot be recovered**. What survives is the job's three annotations: two
  `set-output` deprecation warnings and one failure, "Process completed with exit code 1".
  The warnings can only come from the two `::set-output` lines at the end of `Get WPID`,
  so the run reached at least that far. Where it stopped after that is unknown.
- Neither `sandbox-wp-db` nor `sandbox-wp.gh.io` contains a "Publish approved pathway" or
  "Add files for approved pathway" commit, so **the run pushed nothing anywhere**. The
  three pushes come before the `gh pr edit --add-body` step, so that step was never
  reached, whatever else is true of it.

**Consequently:** the fixes below are read out of the YAML. Each is a defect in its own
right, and none of them is offered as the cause of that run. The first successful run will
very likely turn up something none of us saw by reading.

The submission app depends on this pipeline. The app opens the PR and gives curators a
dashboard, but publication and WPID assignment belong to the target repository. If 3A does
not work, an approved submission never becomes a pathway.

## What changed in 3A

1. **The WPID was parsed out of a full path, and the failure is silent.**
   `sed -E 's/WP([0-9]+)__PR.*/\1/'` over `_drafts/WP5464__PR61.md` yields `_drafts/5464`,
   not `5464`. The next line, `[ "$WPID_NUM" -eq "0" ]`, does print "integer expression
   expected" — but a failing command in an `if` **condition** is exempt from `set -e`, and
   the original does not set `-e` anyway, so control simply falls to the else branch. There
   `WPID_NEW=$((WPID_NUM))` evaluates `_drafts/5464` as arithmetic, reads the unset name
   `_drafts` as 0, and assigns **0**. An edit would publish as **WP0**. For a new
   submission the same expression is `_drafts/0`, a division by zero, so `WPID_NEW` is
   never assigned at all. Reproduced in a plain shell. Fixed by taking the `basename`
   first and refusing anything that is not a number, rather than letting it become one.
2. **`gh pr edit --add-body` is not a flag.** `gh pr edit` has `--body` and `--body-file`
   and no append (`gh pr edit --help`). The step would exit 1 and take the `Close PR` step
   behind it with it. Replaced by reading the current body with `gh pr view --json body`
   and writing back the concatenation.
3. **`::set-output` is deprecated.** It still worked on the 2025-09-03 run — the two
   annotations it left are warnings, "will be disabled soon" — but GitHub has said it will
   stop working, so it is replaced by `>> "$GITHUB_OUTPUT"`.
4. **`sandbox-wp.gh.io` was checked out with `token: ${{ secrets.GITHUB_TOKEN }}`.** All
   three repositories are public, so that token can *read* them; what it cannot do is
   *push* to a repository other than the one the workflow runs in. 3B does the same
   checkout with `ssh-key: ${{ secrets.ACTIONS_SANDBOX_DEPLOY_KEY }}`; 3A now matches it.
5. **No `permissions:` block**, so the job took whatever the repository default is. Now
   `contents: write` and `pull-requests: write`, which is everything it does — PR comments
   and PR labels both fall under `pull-requests`, so `issues: write` is not needed.
6. **No `git pull --rebase` before the three pushes.** Workflow 1 pushes drafts to
   `sandbox-wp.gh.io` on every processed pull request, so a concurrent commit is ordinary
   and would make 3A's push non-fast-forward. 3B already rebases. The three checkouts also
   move to `fetch-depth: 0`: on the default depth-1 clone the local history is a single
   grafted commit and the rebase cannot be relied on, which is precisely the case the
   rebase exists for.
7. **The commit messages said `WP${{ steps.get_wpid.outputs.wpid }}`** while the output
   already carries the prefix, producing `WPWP1234`. The bare `git commit` also exits 1
   when there is nothing staged; `|| echo "No changes to commit"` matches 3B.
8. **`find` and `set -e` do not mix by default.** The strict mode this file now sets would
   have changed behaviour in three places, so each one is written to tolerate what the
   original tolerated: `find … | head -n 1` becomes `find … -print -quit` (with enough
   matching lines, `head` closing the pipe kills `find` with SIGPIPE and `pipefail` turns
   that into exit 141 — reproduced), `ls | grep | sort | tail` gets `|| true` plus an
   explicit emptiness check (`grep` exits 1 on no match), and the loops over
   `_data/drafts` and `draft_assets` are guarded with `[ -d … ]` and read from a process
   substitution, because a `find` on a missing directory exits 1 and would otherwise abort
   the step.

Beyond the defect list, 3A gains three things it did not have:

- A **guard** that refuses to publish when `pathways/WP<new>/` already exists on `main`
  for a newly allocated id, which would otherwise overwrite a published pathway.
- A **check that the draft page was actually found**, by counting the files the first loop
  moved. Testing the target directory for emptiness instead would be a no-op for an edit,
  since that directory already holds the previous publication.
- The **publish marker comment and its failure counterpart**, described below.

Two things deliberately *not* added:

- **No `concurrency:` group.** Serialising publication would stop two approvals reading
  the same `max(WP*)`, but GitHub keeps at most one *pending* run per group: with one run
  going and one queued, a third approval cancels the queued one, and a cancelled run never
  reaches the `if: failure()` step — that submission would vanish with no comment and no
  label. A duplicate id surfaces loudly instead: the second run either finds the target
  directory already there, or ends up adding the same file path as the first, so its
  rebase conflicts and its push fails with a comment on the PR.
- **No check for the `test pathway` label.** 3A does not look at it and this change does
  not add it, so applying `accepted` to a pathway labelled "do not publish" still
  publishes it. Worth deciding upstream; it is not something to slip in silently.

## What changed in the dispatcher

1. **The trigger moves from `pull_request` to `pull_request_target`.** Most submissions
   here come from a fork, and for a `pull_request` event on a fork GitHub caps
   `GITHUB_TOKEN` at read — a `permissions:` block cannot raise it — so `gh workflow run`
   is refused and the label does nothing. This is the one part of the pipeline where the
   evidence survives: run `26719401516` (2026-05-31, `accepted` on PR #45, head
   `mkutmon/sandbox-wp-db`) logs every token scope as read and then
   `HTTP 403: Resource not accessible by integration` on the dispatch. The two runs that
   succeeded, `16786503153` and `17838390305`, both came from branches inside
   `wikipathways/sandbox-wp-db` itself. A `pull_request_target` run of workflow 1 on
   2026-07-15 shows the repository's own default is `Actions: write`, so the read-only
   token on the fork run is the fork cap and not a repository setting. Two further
   dispatcher failures (2025-08-20 and 2025-09-18) came from in-repo branches and their
   logs are expired, so those have no explanation.
   `pull_request_target` is only safe here because the job checks nothing out and runs no
   code from the pull request — it reads two fields off the event and calls the API. Do
   not add a checkout step to it.
2. **An explicit `permissions:` block** (`actions: write`, `contents: read`). In the base
   context the token is not capped, so today this only narrows what it gets; it also keeps
   the workflow working if the repository default is ever tightened.
3. **The `resubmitted` case passed the wrong input name.** It sent `-f pr_number=`, but
   workflow 1's `workflow_dispatch` input is `manual-pr-number`, so GitHub rejected the
   call. The name is corrected and the case kept: workflow 1's `pull_request_target`
   trigger is filtered to `paths: ['**/*.gpml']`, so a push that does not touch a GPML
   never reprocesses a submission, and this label is the only way to ask for it by hand.
4. **`${{ github.event.label.name }}` was interpolated straight into the shell.** Labels
   are repository-controlled so this is hardening rather than a live hole, but it is the
   same shape the app's own `mvp1/pr-preview.yml` documents avoiding. Values now travel
   through `env:`.

## The marker comment

3A posts a comment carrying a machine-readable marker:

```
<!-- wikipathways-publish {"pr":54,"wpid":5678,"status":"published"} -->
Published as [WP5678](https://sandbox.wikipathways.org/pathways/WP5678).
```

and, on failure, an `if: failure()` counterpart:

```
<!-- wikipathways-publish {"pr":54,"status":"failed","step":"push-sandbox-wp-db"} -->
```

This is the contract between the pipeline and the submission app. The app must not recover
the assigned WPID by parsing English out of the PR description: workflow 1 overwrites the
description with `gh pr edit --body` on every run, so anything written there disappears the
next time the submitter pushes. Comments survive. The `published` and `publish failed`
labels make the same state visible in GitHub's own UI.

The failure comment says what actually happened, which depends on how far the run got. The
draft files are **moved**, not copied, and `sandbox-wp.gh.io` is pushed first, so:

- Failure before that push: nothing was pushed anywhere. 3A **removes the `accepted`
  label** so that applying it again re-fires `labeled` and starts a fresh run — without
  that, re-approving does nothing, since re-applying a label the PR already carries emits
  no `labeled` event.
- Failure after it: the drafts have already been moved into the published folders of the
  website repository and `sandbox-wp-db` did not get them. A re-run stops at `Get WPID`.
  The comment says so, and the label is left in place; the two repositories have to be
  brought back in line by hand.
- Failure after both pushes: the pathway is published and only the tail of the workflow
  did not finish. Nothing needs re-running.

## Testing it

`workflow_dispatch` **runs the copy of the workflow file on the ref you dispatch**, not
the copy on the default branch:

```bash
gh workflow run 3a_approved_pull_request.yml \
  -R wikipathways/sandbox-wp-db --ref fix/publish-workflow -f pr_number=NN
```

The default-branch rule that is easy to confuse this with governs only whether a workflow
is *dispatchable at all* — a workflow that exists nowhere on the default branch cannot be
started this way. 3A is already on `main`, so it is dispatchable, and a rewritten copy on
a branch can be run without merging first. Test on the branch.

That is also the manual fallback when a run has failed and the label route is not
available: dispatching 3A by hand takes the same `pr_number` the dispatcher would pass.

Two cautions about the first run:

- **It publishes for real.** 3A pushes straight to `main` in two repositories and closes
  the PR; there is no dry-run mode and no undo beyond a revert commit. Use a submission
  that is meant to be published and that you are willing to clean up.
- **Do not use a pathway labelled `test pathway`.** That label reads "This is a test or
  tutorial pathway; do not publish", and 3A does not check for it, so publishing one is
  exactly what would happen.

The dispatcher cannot be tested the same way. `pull_request_target` takes the workflow
file from the **base** branch, so the change only takes effect once it is on `main`; after
that it applies to already-open pull requests immediately.

## Opening the pull request

Marvin has push access to `sandbox-wp-db` (`push: true`, verified), so a branch in the
repository works and no fork is needed. From a clone of the target repository:

```bash
git clone git@github.com:wikipathways/sandbox-wp-db.git
cd sandbox-wp-db
git checkout -b fix/publish-workflow

CURATOR=~/Documents/Services/WikiPathways/wikipathways-curator
cp "$CURATOR"/sandbox-workflows/.github/workflows/*.yml .github/workflows/

git add .github/workflows
git commit -m "Repair the approved-PR publish workflow and the label dispatcher"
git push -u origin fix/publish-workflow
gh pr create --fill
```

Then create the two labels from `labels.md`.

## What still needs someone with more access

**A write credential for `sandbox-wp-assets`.** 3A pushes to
`wikipathways/sandbox-wp-assets`, and Marvin has read-only access there
(`push: false`, verified). The workflow expects a deploy key with write access under
`ACTIONS_SANDBOX_ASSETS_DEPLOY_KEY`, matching what `ACTIONS_SANDBOX_DEPLOY_KEY` already
does for `sandbox-wp.gh.io`. Only an admin on `sandbox-wp-assets` can create it.

Until that secret exists the expression is empty, `actions/checkout` falls back to the
job's `GITHUB_TOKEN`, and the checkout of that public repository succeeds — it is the push
that cannot work. Both steps are `continue-on-error`, so a publication is not held up by
it. The cost of running without the credential is worth stating plainly: the draft assets
are **moved** out of `draft_assets/`, so once the website repository is pushed, the SVG —
the one file that is not also copied into `sandbox-wp-db` — is no longer in the working
tree of any repository. It stays in `sandbox-wp.gh.io`'s git history and can be recovered
from there, but it is not published anywhere until the assets push works. This is how the
original behaves too; it is not introduced here.

`ACTIONS_SANDBOX_DEPLOY_KEY` needs no such confirmation: `1_on_pull_request.yml` uses the
same secret to push to `sandbox-wp.gh.io`, and the most recent such commit is 2026-07-15,
so the key still has write access.

## How the claims here were checked

```bash
# The one 3A run: event, actor, timing, and the fact that its logs are gone
gh api repos/wikipathways/sandbox-wp-db/actions/runs/17442557461 \
  --jq '{event, actor: .actor.login, head_branch, created_at, updated_at, conclusion}'
gh api repos/wikipathways/sandbox-wp-db/actions/runs/17442557461/jobs \
  --jq '.jobs[] | {name, conclusion, steps: (.steps | length)}'
gh api repos/wikipathways/sandbox-wp-db/actions/runs/17442557461/logs        # HTTP 410
gh api repos/wikipathways/sandbox-wp-db/check-runs/49528874274/annotations \
  --jq '.[] | {level: .annotation_level, message}'

# Nothing was ever published
gh api 'repos/wikipathways/sandbox-wp-db/commits?per_page=100' --jq '.[].commit.message'
gh api 'repos/wikipathways/sandbox-wp.gh.io/commits?per_page=100' --jq '.[].commit.message'

# The dispatcher: which runs failed, from where, and the one surviving log
gh run list -R wikipathways/sandbox-wp-db --workflow pr_label_dispatcher.yml --limit 10 \
  --json databaseId,conclusion,createdAt,displayTitle
gh api repos/wikipathways/sandbox-wp-db/actions/runs/26719401516 \
  --jq '{head_repository: .head_repository.full_name, conclusion}'
gh api repos/wikipathways/sandbox-wp-db/actions/runs/26719401516/attempts/1/logs > log.zip

# The repository default token permissions, seen from a pull_request_target run
gh api repos/wikipathways/sandbox-wp-db/actions/runs/29390429466/logs > wf1.zip
# ... GITHUB_TOKEN Permissions: Actions: write, Contents: write, PullRequests: write ...

# All three repositories are public, and what access we have
for r in sandbox-wp-db sandbox-wp.gh.io sandbox-wp-assets; do
  gh api repos/wikipathways/$r --jq '.name + " " + .visibility'
  gh api repos/wikipathways/$r --jq .permissions
done

# The label vocabulary, including the colours and `test pathway`'s description
gh api repos/wikipathways/sandbox-wp-db/labels --jq '.[] | "\(.name)\t\(.color)\t\(.description)"'

# `gh pr edit` has no --add-body
gh pr edit --help
```

The shell behaviour claims — the `-eq` mis-assignment, the SIGPIPE exit 141, `find` on a
missing directory under `set -e` — were reproduced in a local shell rather than reasoned
about, and the `Get WPID` and `Rename and Move Files` blocks were run against a fixture
tree for a new pathway, an edit, an edit over an existing publication, a missing
`_data/drafts`, a missing `draft_assets`, an empty `_pathways/`, an unparseable slug, and
a PR with no draft.
