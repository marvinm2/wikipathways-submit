# The sandbox-wp-db pipeline

Read first-hand from the five workflow files and from the GitHub API on 2026-07-27. Section 8
lists the commands, so the next person can re-check rather than trust this page. Where something
could not be checked, this page says so instead of guessing at a cause.

Sections 1-6 describe the **upstream** repo. Section 7 covers what is different on the fork the
app actually points at, and is the one to read if you are wondering why a run went red.

## 1. What this is

[`wikipathways/sandbox-wp-db`](https://github.com/wikipathways/sandbox-wp-db) is not a plain
content repo. It owns publication: its own GitHub Actions convert a submitted GPML into the full
set of derived files, push them into a public drafts area for review, and, on an `accepted`
label, assign the WPID, copy everything into the published folders of three repositories, and
close the pull request. Nothing in that chain merges anything. Our app is the front door and the
curator's screen: it opens the PR as the submitter, shows the before/after render and the
checklist, and approves by applying a label. The publication itself belongs to the repo.

## 2. The five workflows

### `1_on_pull_request.yml` — "1. On Pull Request"

**Trigger.** `pull_request_target` filtered to `paths: ['**/*.gpml']`, plus `workflow_dispatch`
with an input named `manual-pr-number` (default `'10'`). `pull_request_target` runs in the base
repo's context, so the jobs get the base repository's `GITHUB_TOKEN` and can see repository
secrets, which is how the workflow can edit the PR and how `commit-outputs` can use the
`sandbox-wp.gh.io` deploy key. Concurrency is grouped by head ref with `cancel-in-progress:
false`, so repeated pushes queue rather than clobber each other.

**What it does.** Ten jobs: `get-pr`, `get-gpml`, `metadata`, `authors`, `pubmed`, `frontmatter`,
`json-svg`, `testing`, `commit-outputs`, `update-pr-desc`. They run in a chain, not a wide fan-out:
`get-pr` → `get-gpml` → `metadata`, and only then do `authors`, `pubmed`, `frontmatter`,
`json-svg` and `testing` run in parallel, all five declaring `needs: [get-pr, get-gpml, metadata]`
(workflow lines 407, 525, 597, 669, 768). `metadata` is therefore a single point of failure for
everything downstream. Results are handed forward as workflow artifacts (`gpml-file`, `metadata`,
`authors`, `pubmed`, `frontmatter`, `json-svg`), each with a one-day retention.

`get-gpml` is the one to understand. It finds the pathway with

```bash
GPML_FILEPATH=$(gh pr view $PR_NUMBER --json files --jq '.files.[].path' | grep '.gpml$')
```

which assumes exactly one GPML in the diff. With two, `$GPML_FILEPATH` holds two lines and the
job dies. That is not hypothetical: PR #58 touches `pathways/WP1072/WP1072.gpml` and
`pathways/WP179/WP179.gpml`, and run `28753193790` failed in `get-gpml` with

```
cp: cannot stat 'pathways/WP1072/WP1072.gpml'$'\n''pathways/WP179/WP179.gpml': No such file or directory
```

**One GPML per pull request** is a hard rule of this pipeline, not a convention.

Classification then happens on the basename, but only after the basename has been rewritten. Two
steps, in this order (workflow lines 99-115):

```bash
# 1. a name containing '__' collapses to its prefix
if [[ $GPML_FILE == *__* ]]; then
  GPML_FILE="${GPML_FILE%%__*}.gpml"
fi

# 2. only then, the edit test
if [[ $GPML_FILE =~ ^WP[1-9][0-9]{0,4}\.gpml$ ]]; then   # an edit
```

The first step is marked `TEMPORARY: Rename previously processed test GPMLs`, and it changes the
answer for anything that has already been through the pipeline. `WP1001__PR60.gpml` collapses to
`WP1001.gpml` and classifies as an **edit**, not as a new submission. Only after that collapse
does the regex decide: a basename matching `WP<1-5 digits, no leading zero>.gpml` is an edit and
is copied to `WP<id>__PR<n>.gpml`; anything else is a new submission and becomes `WP0__PR<n>.gpml`.
The directory the file sits in is never consulted, so `pathways/WP0001/WP0001.gpml` classifies as
**new**, because the leading zero fails the regex. That is what we want from the placeholder, and
worth knowing it is a property of the regex rather than a decision anyone made. The stem of the
renamed file is the **slug** that every later path is built from. From here on, `<slug>` means
`WP0__PR<n>` for a new pathway and `WP<id>__PR<n>` for an edit.

`get-gpml` also pulls `Name`, `Organism` and the `WikiPathways-description` comment out of the
GPML with `xmllint`, writes a first PR body ("Processing..."), and, in a step marked TODO, pushes
the head ref into the base repo if it is not already there.

`metadata` runs [`meta-data-action`](https://github.com/wikipathways/meta-data-action) v1.1.4
under Java 11 against cached BridgeDb `.bridge` files, producing `<slug>-info.json`,
`<slug>-datanodes.tsv` and `<slug>-refs.tsv`. The cache lists 36 globs, 35 of them organism
prefixes plus one `metabolites*.bridge`. `pubmed` turns `refs.tsv` into `<slug>-bibliography.tsv`
via `scripts/generate-references`. `frontmatter` turns `info.json` into `<slug>.md` via
`scripts/create_pathway_frontmatter.py`. `json-svg` runs `scripts/generate-svgs/gpmlconverter` on
Node 18 and pinned `ubuntu-22.04` (a comment says newer runners break Puppeteer), yielding
`<slug>.json`, `<slug>.svg`, `<slug>.png` and `<slug>-thumb.png`. `authors` diffs the GPML's
`Author=[...]` list against `scripts/author_list.csv` and writes a profile stub for anyone new.

`testing` is three checks over `gh pr diff`: title at least 10 characters, description at least 15
words for a new pathway (or changed by no more than 3 words / 10 characters for an edit), and
whether any `<DataNode>` lines were added, modified or deleted. Each yields PASS or REVIEW
REQUIRED. **None of them gates anything.** The result is coloured text in a table and, where
REVIEW REQUIRED, an extra unchecked box in a reviewer checklist appended to the PR body.

**What it writes where.** `commit-outputs` checks out `wikipathways/sandbox-wp.gh.io` with
`ssh-key: ${{ secrets.ACTIONS_SANDBOX_DEPLOY_KEY }}` (workflow line 1041), copies the draft
artifacts listed in section 5 into place, commits, rebases and pushes. The deploy key, not the
`pull_request_target` token, is what makes that cross-repo push possible. `update-pr-desc` then
concatenates the per-job report fragments and **overwrites** the PR body with `gh pr edit --body`,
including a link to `https://sandbox.wikipathways.org/drafts/<slug>`. Note that `json-svg` is
deliberately absent from that job's `needs:` list ("add once that step is resolved"), so its
fragment interpolates empty.

The body is written twice per run, once at the start of `get-gpml` and once at the end. **Anything
written into the PR description by anyone else is destroyed on the next run.** PR comments are not
touched, which is why the app/pipeline contract in section 3 uses a comment.

`update-pr-desc` is also where the pipeline's one security defect lives; see section 6.1.

**Reliability.** Of the last eight runs, four failed: three in `metadata`, one in `get-gpml` (the
two-GPML PR above). The most recent `metadata` failure, run `29390758046`, ends with

```
Exception in thread "main" java.lang.NullPointerException
	at meta.data.action.MetaDataExtractor.main(MetaDataExtractor.java:108)
```

so the failure is inside `meta-data-action` rather than in the workflow's own shell. Because the
whole downstream fan-out needs `metadata`, a failure there means no drafts appear and the PR body
is left saying "Processing...". This is never a hard gate on approval: a curator can still label
`accepted` on a PR whose processing failed, and 3a will then find no draft and exit 1.

### `2_after_pr_processed.yml` — "2. After PR Processed"

**Trigger.** `workflow_run` on workflow 1, `types: [completed]`.

**What it does.** Nothing. It is 21 lines: an `on-success` job that runs
`echo 'The triggering workflow passed'` and an `on-failure` job that echoes the opposite. Its own
header comment says "It is used to set labels and reviewers on the pull request", which is not
true of the code as written. Treat the comment as a statement of intent and the file as a stub.

**It has never run.** `gh run list --workflow 2_after_pr_processed.yml` returns an empty list:
zero runs, ever. One thing in the file would account for that, though with no run and no log
there is nothing to confirm it against: the `workflows:` filter is spelled
`[1_on_pull_request.yml]`, a file name, where the `workflow_run` trigger matches on the workflow's
`name:`, which is "1. On Pull Request". Nothing depends on this workflow either way.

### `3a_approved_pull_request.yml` — "3A. Approved Pull Request"

**Trigger.** `workflow_dispatch` only, with a required `pr_number` input. It is meant to be
started by the label dispatcher; the only run it has ever had was a hand dispatch (see below).

**What it does.** Checks out all three repos side by side, then:

1. **Get WPID.** Finds `_drafts/WP*__PR<n>.md` in the website repo; exits 1 if there is none. If
   the id part of the slug is `0`, the new id is `max(_pathways/WP<n>.md) + 1` computed with
   `ls _pathways/ | grep -E '^WP[0-9]+\.md$' | sort -V | tail -n 1`; otherwise the existing id is
   kept. **This is where a WPID is born.** Nothing before this point reserves or claims one.
2. **Rename and move.** For the `.md`: copy to `sandbox-wp-db/pathways/WP<new>/WP<new>.md` and
   move to `sandbox-wp.gh.io/_pathways/WP<new>.md`. For the `.tsv` files: move
   `_data/drafts/<slug>-*.tsv` to `_data/<WP<new>>-*.tsv`. For everything in
   `draft_assets/<slug>/`: copy into `sandbox-wp-db/pathways/WP<new>/` (skipping the SVG) and move
   into `sandbox-wp-assets/pathways/WP<new>/`.
3. **Three pushes**, one per repo, straight to `main`. No pull requests, no rebase.
4. **Append the WPID to the PR description**, then **`gh pr close`**.

It never merges. A published pathway's PR ends up *closed*, and the branch that carried the GPML
is never part of `main`. The GPML that lands in `pathways/WP<new>/` is the copy that travelled
through `draft_assets`, not the submitted file.

**Its one run, in full.** Run `17442557461`, 2025-09-03T18:25:34Z to 18:25:53Z, so nineteen
seconds, conclusion `failure`. Event `workflow_dispatch`, actor and triggering actor `egonw`,
head branch `main`: a hand dispatch, not a label dispatch. The run's logs have expired (the API
returns HTTP 410) and its `steps` array comes back empty, so **the failing step cannot be
recovered**. What can be checked is the outcome: neither `sandbox-wp-db` nor `sandbox-wp.gh.io`
contains any "Publish approved pathway" or "Add files for approved pathway" commit, so the run
pushed nothing. Section 6 lists defects that would each break this workflow, but none of them is
established as the cause of that particular failure. **Nothing has ever been published by 3a.**

### `3b_rejected_pull_request.yml` — "3B. Rejected Pull Request"

**Trigger.** `workflow_dispatch` with a required `pr_number`.

**What it does.** Checks out `sandbox-wp.gh.io` with the deploy key, deletes every file matching
`WP*__PR<n>*` under `_drafts`, `_data/drafts` and `draft_assets`, commits (tolerating an empty
commit), `git pull --rebase`, pushes. Then comments "This pull request has been rejected. Please
review the comments and make necessary changes before resubmitting." and closes the PR.

It is the shorter workflow and the more correct one: it uses the deploy key for the cross-repo
checkout, tolerates an empty commit, and rebases before pushing. Where 3a and 3b disagree, 3b is
the one doing it right. It has also actually run, repeatedly, including successfully.

### `pr_label_dispatcher.yml` — "PR Label Dispatcher"

**Trigger.** `pull_request: types: [labeled]`.

**What it does.** Reads `github.event.label.name` and maps it to a workflow:

| Label | Dispatches |
|---|---|
| `accepted` | `3a_approved_pull_request.yml` |
| `rejected` | `3b_rejected_pull_request.yml` |
| `resubmitted` | `1_on_pull_request.yml` |

with `gh workflow run "$workflow" -R "${{ github.repository }}" -f pr_number="$pr_number"`. Any
other label exits 0 without doing anything. The `case` block is fifteen lines (file lines 18-32)
and is the entire control surface between a curator's intent and the publication machinery, which
is what makes an app that only applies labels a viable design.

**It has worked, and lately it has not.** Six runs. Run `17838390305` (2025-09-18T18:48:10Z)
succeeded and dispatched 3b run `17838392530` six seconds later, triggering actor
`github-actions[bot]`, also successful. Label to dispatch to workflow run is a demonstrated path,
not a theory. Run `16786503153` (2025-08-06) also succeeded. The other four failed in the
`Dispatch based on label` step. Only one of those four still has logs, and it is explicit:

```
2026-05-31T17:44:55 Triggering workflow: 3a_approved_pull_request.yml
2026-05-31T17:44:56 could not create workflow dispatch event: HTTP 403: Resource not accessible
                    by integration
```

That is run `26719401516`, an `accepted` label on PR #45, which is **still open and still carries
the `accepted` label**. A curator approved that submission on 2026-05-31 and nothing happened.
The 403 is what a missing `actions: write` scope looks like; the other three failures are older
and their logs are gone, so whether they failed the same way is unknown. The file's last commit is
`df1172cd`, 2025-09-18T18:45:57Z, three minutes before the successful run, so the workflow itself
is not what changed between that success and the 2026 failures. What did change cannot be read
from here: the repository's default workflow-permissions setting needs admin access and the API
returns 403. An explicit `permissions:` block makes the
workflow independent of that setting either way.

## 3. The contract

| Transition | Who does it | How it is signalled |
|---|---|---|
| Submission arrives | The app, as the submitter (OAuth) | A branch `WP0001_<username>_<stamp>` holding `pathways/WP0001/WP0001.gpml`, and a PR against `main` |
| The PR is processed | `1_on_pull_request.yml`, automatically | The PR body is **overwritten** with the metadata and testing tables and a link to `https://sandbox.wikipathways.org/drafts/<slug>`; draft files appear in `sandbox-wp.gh.io` |
| Re-upload of a revision | The app, pushing to the same head branch | The `synchronize` activity re-triggers workflow 1; the drafts and the PR body are regenerated in place |
| Curator approves | The app, as the bot | It **applies the `accepted` label**. That is the whole action: no merge, no commit, no immediate result |
| Approval becomes a workflow run | `pr_label_dispatcher.yml` | `gh workflow run 3a_approved_pull_request.yml -f pr_number=N` |
| The WPID is assigned | `3a_approved_pull_request.yml`, at publication time | `max(_pathways/WP<n>.md) + 1`, computed inside the run. Nothing reserves an id before this moment |
| Publication | `3a_approved_pull_request.yml` | Direct pushes to `main` of `sandbox-wp-db`, `sandbox-wp.gh.io` and `sandbox-wp-assets`, then a marker comment (below) |
| Rejection | The app applies `rejected`; `3b_rejected_pull_request.yml` acts | Drafts deleted, a rejection comment posted, PR closed |
| The PR closes | 3a or 3b, with `gh pr close` | Closed, **never merged** |

Two things in that table are easy to misread, so state them plainly.

**Nothing is ever merged.** Not by us, not by the pipeline. `approve_and_merge` has no counterpart
here; the app's `merged_at` webhook path will never fire for a published pathway. The terminal
state of a successful submission is a *closed* PR plus three pushes to three `main` branches. The
lock release and review terminalisation must key off `closed`, not `merged`.

**Approve is a label, not an action with a result.** When a curator clicks approve, the app gets a
201 from the labels endpoint and knows nothing more. The dispatcher fires on a separate event, 3a
runs for a couple of minutes, and it may fail, or the dispatch may be rejected outright as it was
on PR #45. The dashboard has to model "approved, publishing" as a distinct state from "published",
and has to learn the outcome from GitHub rather than from its own write.

That is what the marker comment is for. Recovering the WPID by parsing English out of the PR
description is not an option, because workflow 1 overwrites the body on every run, so 3a's
appended sentence has a shelf life of exactly one `synchronize` event. Comments survive. The
publish workflow therefore posts:

```
<!-- wikipathways-publish {"pr":54,"wpid":5678,"status":"published"} -->
Published as [WP5678](https://sandbox.wikipathways.org/pathways/WP5678).
```

and, from an `if: failure()` step:

```
<!-- wikipathways-publish {"pr":54,"status":"failed","step":"..."} -->
```

plus a human-readable line saying what broke. The HTML comment is the machine-readable half:
invisible in the rendered comment, parseable with one regex, and stable across re-runs because the
app upserts on the marker. The visible half is for whoever is reading the PR on GitHub. The two
new labels in section 4 make the same state visible in GitHub's own list view without anyone
having to open the PR.

## 4. Labels

The repo defines eleven labels (checked, 2026-07-27). Only two of them do anything.

**Load-bearing.** The dispatcher turns these into workflow runs, so applying one is an
irreversible action, not an annotation:

| Label | Description in the repo | Effect |
|---|---|---|
| `accepted` | Ready for publication | Dispatches 3a: assigns the WPID, publishes to three repos, closes the PR |
| `rejected` | The submission is rejected by a reviewer | Dispatches 3b: deletes the drafts, comments, closes the PR |

`resubmitted` ("Revisions ready for processing") is *meant* to be load-bearing, since the
dispatcher maps it to workflow 1, but the dispatch call names an input workflow 1 does not have
(defect 8) and is redundant anyway: pushing to the head branch re-triggers workflow 1 through
the default `synchronize` activity type.

**Descriptive.** Nothing reads these; they exist so a human scanning the PR list can see state.
Workflow 1's TODO comments show they were supposed to be applied automatically, and workflow 2's
header claims it sets them, but no workflow applies any label today:

| Label | Meaning |
|---|---|
| `new pathway submission` | Submission of a new pathway |
| `edited pathway submission` | Submission of an edit to an existing pathway |
| `processing` | Being processed by the initial GH action |
| `tests passed` | All tests have passed |
| `tests failed` | One or more tests failed |
| `review required` | Review required |
| `author feedback required` | Feedback from the author is required |
| `test pathway` | This is a test or tutorial pathway; do not publish |

`test pathway` is the one descriptive label with teeth in practice: most of what is currently in
the sandbox is test traffic from the PathVisio plugin, and a curator needs to be able to say
"never publish this" without rejecting it.

**Two new ones we add**, so that publication state is legible in GitHub's own UI and not only in
our dashboard:

| Label | Meaning |
|---|---|
| `published` | 3a completed; the marker comment carries the assigned WPID |
| `publish failed` | 3a ran and failed; the marker comment names the step |

**Neither of these exists in the repository yet.** The label list above is the complete set, and
these two are not in it. They have to be created by hand, by someone with push access, before the
repaired 3a can use them: `gh pr edit --add-label` fails on an unknown label. The exact
`gh label create` commands, with colours, are in `sandbox-workflows/labels.md`. In the repaired
3a both labelling steps are `continue-on-error`, so a missing label never blocks a publication;
it just means the state is invisible in the PR list, which is the whole point of adding them.

Neither new label is read by the dispatcher, so adding them cannot trigger anything. A label
applied with `GITHUB_TOKEN` does not start a workflow run in any case, so 3a cannot re-trigger
itself.

## 5. Draft artifacts

Everything workflow 1 produces lands in `wikipathways/sandbox-wp.gh.io` on `main`. All three
sandbox repos are **public** (`private: false`, checked), so the drafts are readable anonymously
over `raw.githubusercontent.com` and the unauthenticated REST API; a `curl` of a draft `.md`
returns 200 with no token. That matters for the app: the before/after preview and any metadata we
want to show can be fetched without spending the bot's rate limit or holding a credential for a
repo we otherwise never touch.

With `<slug>` being `WP0__PR<n>` for a new pathway and `WP<id>__PR<n>` for an edit:

| Path | What it is |
|---|---|
| `_drafts/<slug>.md` | The Jekyll page for the pending pathway, frontmatter generated from `info.json`. This is the file 3a looks for to decide a PR is publishable |
| `_data/drafts/<slug>-datanodes.tsv` | One row per data node with its database annotation |
| `_data/drafts/<slug>-bibliography.tsv` | Literature references resolved against PubMed |
| `draft_assets/<slug>/<slug>.gpml` | The submitted GPML, renamed |
| `draft_assets/<slug>/<slug>.json` | pvjson, pretty-printed |
| `draft_assets/<slug>/<slug>.svg` | The rendered pathway |
| `draft_assets/<slug>/<slug>.png` | Raster render |
| `draft_assets/<slug>/<slug>-thumb.png` | Thumbnail |
| `draft_assets/<slug>/<slug>-info.json` | Pathway metadata from `meta-data-action` |
| `draft_assets/<slug>/<slug>-datanodes.tsv` | As above, copied here too |
| `draft_assets/<slug>/<slug>-refs.tsv` | Raw reference ids, input to the bibliography |
| `draft_assets/<slug>/<slug>-bibliography.tsv` | As above, copied here too |

New author profiles, when there are any, go to `_authors/*.md` with an updated
`scripts/author_list.csv`.

The rendered page is `https://sandbox.wikipathways.org/drafts/<slug>` and appears roughly five
minutes after the push, once Jekyll rebuilds. Drafts are never cleaned up except by 3b, so
`_drafts` currently holds twelve slugs from PRs going back to #10. A slug present in `_drafts` is
evidence a run once succeeded, not evidence of a live PR.

## 6. Known breakages

Eight defects, found by reading the YAML and probing the API. They are listed worst first. Note
what this table does *not* claim: none of these is established as the cause of the one failed 3a
run, whose logs are gone. Each is a defect that would break the workflow if reached.

Corrected copies of **three** of the five workflows live in `sandbox-workflows/`:
`1_on_pull_request.yml`, `3a_approved_pull_request.yml` and `pr_label_dispatcher.yml`, plus
`labels.md` and a `README.md` explaining how to open the PR. Workflows 2 and 3b have no corrected
copy. These files are ours until someone upstream takes them, and this repo files no PRs against
the wikipathways org without Marvin saying so.

| # | Where | What happens | Fixed |
|---|---|---|---|
| 1 | Workflow 1 | A security defect, reachable by anyone who can open a pull request. Details withheld here — see 6.1 | **No.** `sandbox-workflows/` carries a corrected workflow 1, but it fixes only the three first-contributor defects. This one was reported to the maintainers privately |
| 2 | 3a, "Get WPID" | `WPID_NUM=$(echo $DRAFT_FILE \| sed -E 's/WP([0-9]+)__PR.*/\1/')` runs on the full path `_drafts/WP0__PR54.md`, so the substitution anchors mid-string and yields `_drafts/0`. The next line, `[ "$WPID_NUM" -eq "0" ]`, prints "integer expression expected" but **does not abort**: a failing command in an `if` condition is exempt from `set -e`, so control falls to the else branch and `$((_drafts/0))` follows. Running the exact snippet: a new pathway ends with `WPID=WP` and `old_prefix=WP_drafts/0__PR54`, an edit ends with `WPID=WP0`, and the script exits 0 either way. Silent mis-assignment, not a red step | Yes: `basename` first, then match on `^WP([0-9]+)__PR` |
| 3 | 3a, twice | `echo "::set-output name=..."`. GitHub deprecated the command in 2022 and the runners warn on it. Whether the outputs on the 2025-09-03 run were actually empty cannot be checked, because that run's logs have expired | Yes: `>> "$GITHUB_OUTPUT"`, which removes the question |
| 4 | 3a, the `sandbox-wp.gh.io` and `sandbox-wp-assets` checkouts and pushes | Both checkouts pass `token: ${{ secrets.GITHUB_TOKEN }}`, which is scoped to `sandbox-wp-db`. Both sister repos are public, so the **checkout still reads fine**; it is the **push** that has no write credential. 3b does the same cross-repo checkout with `ssh-key: ${{ secrets.ACTIONS_SANDBOX_DEPLOY_KEY }}` and can push | Partly: the `.gh.io` checkout switches to the deploy key, matching 3b. The assets repo needs its own credential (see below) |
| 5 | 3a, the three push steps | No `permissions:` block and no `git pull --rebase` before any of the three pushes. 3b has the rebase. Two approvals close together, or any push to those repos in the interim, and 3a loses the race | Yes: explicit `permissions:`, a concurrency group, and a rebase before each push |
| 6 | 3a, "Append Message to PR Description" | `gh pr edit --add-body` is not a real flag (`gh pr edit` has `--body`/`--body-file` and `--add-label`/`--add-reviewer`/`--add-assignee`/`--add-project`, no `--add-body`). The step would exit non-zero, and the "Close PR" step after it would not run. The three pushes happen before it, so a run failing here leaves a published pathway with an open PR and no record of its id. This step is **after** the pushes, and the one 3a run pushed nothing, so that run never reached it | Yes: the step is kept, rewritten to read the current body with `gh pr view --json body` and write the concatenation back with `--body`, and marked `continue-on-error` so it cannot block the close. A `gh pr comment` marker is added alongside it, because the body is overwritten by workflow 1 anyway |
| 7 | `pr_label_dispatcher.yml` | No `permissions:` block. `gh workflow run` needs `actions: write`. The workflow succeeded twice in 2025 and failed four times; the one failure whose log survives (`26719401516`, 2026-05-31, an `accepted` label on PR #45) got `HTTP 403: Resource not accessible by integration`, and that PR is still open and still labelled `accepted` | Yes: explicit `permissions: actions: write, contents: read` |
| 8 | `pr_label_dispatcher.yml`, the `resubmitted` case | It passes `-f pr_number=N`, but workflow 1's `workflow_dispatch` input is named `manual-pr-number`. GitHub rejects a dispatch carrying an unexpected input. Read from the two files; no surviving log shows such a call | Yes, and moot: the case is dropped, because pushing to the PR head already re-triggers workflow 1 via `synchronize` |
| 9 | Workflow 1, the `testing` job's data-node step | The step runs under `bash -e`, and two of its assignments take their exit status from a command substitution that **ends in grep**: `matching_added_node=$(… \| grep "GraphId=\"$graph_id\"")` and `actual_deleted_nodes=$(… \| grep -vF "$safe_modified_nodes")`. grep exits 1 when it matches nothing, so the step dies **with no message at all** — no stderr, just `Process completed with exit code 1`. The first fires on any edit that genuinely deletes a data node (the exact case the test exists to detect); the second on any edit that only re-annotates. `update-pr-desc` and `commit-outputs` both `needs: testing`, so they are skipped and the submission loses its drafts *and* its PR-body report. Found on run `30442228975` and reproduced against PR #8's real diff, where it dies on the fifth deleted node, `GraphId="a57"` | Yes: `\|\| true` on both. Note the counts are still wrong for a separate reason — `modified_nodes` is accumulated with a **literal** `\n` (bash does not expand it inside double quotes, and `echo` does not either), so the filter matches nothing and every deleted node is counted as deleted. That only mis-states a report nothing gates on, so it is left alone rather than rewritten in someone else's workflow |

Alongside the fixes, the corrected 3a gains four things the original does not have: a concurrency
group so two approvals cannot both read the same `max(WP*)`, a guard that refuses to publish a
newly allocated id over an existing `pathways/WP<new>/`, a check that the target directory is
non-empty before pushing, and the marker comment plus `published` / `publish failed` labelling,
including an `if: failure()` counterpart that names the step that broke.

**One standing fact, not a defect** — *superseded 2026-07-29, kept because it was true for a
year and its lesson held.* Upstream, 3a has run exactly once and failed, and there is still no
successful publication in `wikipathways/sandbox-wp-db`'s history. What changed is that the
**repaired** 3a published successfully on the fork (§7), so "what a published pathway looks like"
is now an observed fact rather than a design intention.

The lesson stands, and the first green run collected on it immediately: the repaired 3a carried a
tenth defect that no amount of YAML review had caught, because nothing had ever dispatched it.

| # | Where | What happens | Fixed |
|---|---|---|---|
| 10 | The **repaired** 3a, `push_jekyll` | A `# FIX:` comment inside the `run:` block quoted the old commit message, and that quotation contained a GitHub expression. A `run:` block is a single string value: the runner substitutes expressions into its **text** before bash ever sees it, so a leading `#` protects nothing. The expression did not parse, and an unparseable expression fails the **whole workflow at startup** — `gh workflow run` returns `HTTP 422: failed to parse workflow: (Line: 224, Col: 14): Unexpected symbol: '...wpid'`, and the line it names is the `run:`, not the comment. GitHub also files a zero-job `failure` run named by *path* rather than by workflow name when the file lands, which is the only warning you get | Yes. Note this is the **same mechanism** as defect 1: `${{ }}` is text substitution, not shell expansion. That is worth internalising once rather than meeting twice |

### 6.1 Redacted: an unfixed security defect, reported privately

There is one security defect in workflow 1 — not a bug, a defect that lets a submitted file
influence something it should not. It is unfixed upstream, it is reachable by anyone who can open
a pull request against `sandbox-wp-db`, and it will carry over to the production repository if
workflow 1 is copied there.

**The details that were here have been removed on purpose.** This repository is public, and a
write-up naming the job, the lines and a working payload is a set of instructions for anyone who
reads it before the maintainers have had a chance to act. That was an error in the original
version of this page, not a considered decision: the reasoning for keeping the fix out of the
`sandbox-workflows/` pull request applied just as much to this section, and it was not applied
here.

The full analysis and the fix live outside this repository, in
`../sandbox-wp-db-disclosure-DRAFT.md`, and have been sent to the WikiPathways maintainers
privately. Restore this section once they have shipped a fix, since the write-up is genuinely
useful for anyone maintaining these workflows.

If you are a maintainer and reached this page first, please get in touch and the analysis will
come straight over.

To be straight about what this does and does not achieve: `wikipathways/sandbox-wp-db` is itself
public, so the vulnerable code has always been readable by anyone who cared to look. What the
original section added was the analysis — the job, the lines, why it works and a payload that
does — which is the difference between "readable" and "findable". Removing it narrows that, and
narrows nothing else.

### Two questions we could not settle by reading

**Does anything rewrite the GPML's internal `Version` attribute?** From reading the YAML, no:
workflow 1 renames the file with `cp` and 3a moves it with `mv`, and neither touches the XML. What
is in the repos is consistent with that. `draft_assets/WP0__PR10/WP0__PR10.gpml` still carries
`Version="WP0001_20231104"` inside the file, while a published pathway carries its id in the same
place (`pathways/WP1/WP1.gpml` has `Version="WP1_r117947"`). If nothing rewrites it at
publication, a pathway published from our placeholder keeps `WP0001` in its `Version` forever,
disagreeing with its own filename and directory. Whether that matters depends on what downstream
consumers read the id from, the frontmatter, the filename or the GPML, which we have not traced.
Either 3a gains an `xmlstarlet` edit, or the app rewrites `Version` before the curator approves.

**Who can supply a write credential for `sandbox-wp-assets`?** Marvin has `push` on
`sandbox-wp-db`, `maintain` on `sandbox-wp.gh.io`, and **`pull` only** on `sandbox-wp-assets`
(checked via the API). Defect 4 cannot be fully fixed from where we stand:
`ACTIONS_SANDBOX_DEPLOY_KEY` is a deploy key for the website repo, and 3a's third push needs an
equivalent for assets. Someone with admin on the org has to create it and add it as a secret,
which the corrected 3a expects under `ACTIONS_SANDBOX_ASSETS_DEPLOY_KEY`. Until then the corrected
3a does not reorder the pushes; it marks the assets checkout `continue-on-error: true` and gates
the assets push on `steps.checkout_assets.outcome == 'success'`, so a missing credential skips the
assets copy instead of failing a publication that has already landed in the other two. The asset
files are a mirror of files that also go to `sandbox-wp-db`, the SVG excepted, so skipping them
loses less than aborting mid-publish. Once the secret exists, both steps start working with no
further edit.

## 7. Running the pipeline on a fork

Everything above describes `wikipathways/sandbox-wp-db`. The app is pointed at
**`marvinm2/sandbox-wp-db`**, and a fork does not inherit the parent's Actions secrets. Until
2026-07-29 that was silently fatal to the whole downstream half.

**The symptom.** `commit-outputs` failed on every run the fork had ever had. With
`ACTIONS_SANDBOX_DEPLOY_KEY` empty, `actions/checkout` falls back to `GITHUB_TOKEN`, which is
scoped to `sandbox-wp-db` and cannot write to the site repo:

```
remote: Permission to wikipathways/sandbox-wp.gh.io.git denied to github-actions[bot].
fatal: unable to access '.../sandbox-wp.gh.io/': The requested URL returned error: 403
```

So no draft was ever written, which also explains 3a: its first step is
`find _drafts -name "WP*__PR${PR_NUMBER}.md"`, that came back empty, and it exited 1. Both halves
of "approve and publish" were dead for one reason, and it was not any of the eight defects above.

Note the failure would have been *worse* if it had succeeded: a fork pushing into the upstream
org's site repo would publish a personal test into the shared sandbox website.

**The setup that fixes it**, all of it on Marvin's own account:

| Piece | Value |
|---|---|
| Site repo | `marvinm2/sandbox-wp.gh.io`, forked 2026-07-29 |
| Assets repo | `marvinm2/sandbox-wp-assets`, forked 2026-07-29 |
| Deploy key on the site fork | write-enabled; private half is `ACTIONS_SANDBOX_DEPLOY_KEY` on `marvinm2/sandbox-wp-db` |
| Deploy key on the assets fork | write-enabled; private half is `ACTIONS_SANDBOX_ASSETS_DEPLOY_KEY`, the name the repaired 3a already expected |
| Pages | enabled on the site fork, legacy branch build from `main` |
| `_config.yml` | `baseurl: "/sandbox-wp.gh.io"`, `url: "https://marvinm2.github.io"` — it is project pages now, not an apex domain, so every generated link needs the subpath |

A deploy key is **per repository**; one key cannot serve both forks, which is why there are two.

Every `repository:` in the fork's workflows 1, 3a and 3b now names the forks rather than the org
(commit `6f49c7e0`). The fork also carries the repaired 3a from `sandbox-workflows/`, since that
is the version the assets key was written for.

**What the app needs to match**, or it reads drafts from a repo that no longer receives them:

```
WPSUBMIT_DRAFTS_REPO=marvinm2/sandbox-wp.gh.io
WPSUBMIT_DRAFTS_SITE_BASE_URL=https://marvinm2.github.io/sandbox-wp.gh.io
```

`DraftsReader`'s disk cache is keyed on a hash of repo plus branch, so repointing does not serve
the old target's cached misses; there is nothing to flush.

**Proven, not inferred.** Run `30451444585` on 2026-07-29 is the first run of workflow 1
**anywhere** with all ten jobs green, `commit-outputs` included. It wrote `_drafts/WP0__PR5.md`,
both `_data/drafts/` TSVs and all nine `draft_assets/WP0__PR5/` files, and
`https://marvinm2.github.io/sandbox-wp.gh.io/drafts/WP0__PR5` renders the pathway with its SVG.
`DraftsReader` against that repo returns `available=True` with a resolving `draft_url`, `svg_url`
and `thumb_url`.

### 7.1 The first publication

Run `30460071900`, 2026-07-29, **succeeded — every step green**. This is the first pathway ever
published by 3a anywhere, and it closes the last unobserved step of the lifecycle.

What it did, checked afterwards rather than assumed:

| Claim | Observed |
|---|---|
| WPID assigned at publication time | `5423`, computed as `max(_pathways/WP<n>.md) + 1` over 1000 pages |
| Three pushes, three repos | `pathways/WP5423/` in the db fork (9 files) and the assets fork (9, including the SVG), `_pathways/WP5423.md` in the site fork |
| The assets deploy key works | `Commit and push changes to sandbox-wp-assets` green — the credential defect 4 describes is genuinely closed, not merely routed around |
| Marker comment | `<!-- wikipathways-publish {"pr":5,"wpid":5423,"status":"published"} -->` |
| Labels | `published` applied |
| The PR closes, never merges | `state=CLOSED`, unmerged |
| Drafts are moved, not copied | `_drafts/WP0__PR5.md` now 404s — the draft page is *meant* to disappear on publication |
| The app follows | `GET /api/reviews/5` → `status: published`, `wpid: 5423`, read from the marker comment via the webhook rather than from its own write |

The published page renders at `https://marvinm2.github.io/sandbox-wp.gh.io/pathways/WP5423`.

One cosmetic wrongness on a fork: the marker comment's human-readable half hardcodes
`https://sandbox.wikipathways.org/pathways/WP<id>`, so on the fork it links to the upstream site,
where that pathway does not exist. The machine-readable half — the part the app parses — is
correct, so nothing malfunctions.

**What the approval did not exercise.** The `accepted` label was applied directly rather than
through the dashboard, because PR #5's checklist legitimately fails (`datanodes_mapped`: IRS1
carries no identifier) and the Approve button is correctly disabled. Applying the label is
byte-for-byte what `approve` does, and the dashboard's approve path was already proven live on
2026-07-28, so what remained untested was 3a — which is what this ran. A single end-to-end pass
that starts at the Approve button still has not happened.

Also confirmed in passing, from the 07-28 session: **GitHub emits no `labeled` event for a label
already on the pull request.** The first dispatcher run failed, and re-firing it needed the label
removed and re-added, not simply re-applied.

## 8. How we checked

Everything in sections 1-6 came from these, run on 2026-07-27 against the live repos.

```bash
# The workflows themselves
for f in 1_on_pull_request.yml 2_after_pr_processed.yml 3a_approved_pull_request.yml \
         3b_rejected_pull_request.yml pr_label_dispatcher.yml; do
  gh api repos/wikipathways/sandbox-wp-db/contents/.github/workflows/$f --jq .content | base64 -d
done

# Label vocabulary (eleven; no 'published', no 'publish failed')
gh api repos/wikipathways/sandbox-wp-db/labels --jq '.[] | "\(.name)\t\(.description)"'

# Run history per workflow. 2_after_pr_processed returns []
for w in 1_on_pull_request 2_after_pr_processed 3a_approved_pull_request \
         3b_rejected_pull_request pr_label_dispatcher; do
  gh run list -R wikipathways/sandbox-wp-db --workflow $w.yml --limit 10 \
    --json databaseId,conclusion,createdAt,event,displayTitle
done

# The single 3a run: hand-dispatched by egonw, 19 seconds, logs expired (410), steps empty
gh api repos/wikipathways/sandbox-wp-db/actions/runs/17442557461 \
  --jq '{event, actor: .actor.login, head_branch, created_at, updated_at, conclusion}'
gh api repos/wikipathways/sandbox-wp-db/actions/runs/17442557461/jobs \
  --jq '.jobs[] | {name, conclusion, steps}'

# It pushed nothing
gh api "search/commits?q=repo:wikipathways/sandbox-wp.gh.io+Publish+approved+pathway" \
  --jq .total_count
gh api "search/commits?q=repo:wikipathways/sandbox-wp-db+approved+pathway" --jq .total_count

# The dispatcher works, and the 403 that stopped PR #45
gh api repos/wikipathways/sandbox-wp-db/actions/runs/17838392530 \
  --jq '{event, actor: .actor.login, created_at, conclusion}'
gh api repos/wikipathways/sandbox-wp-db/actions/runs/26719401516/logs > d.zip && unzip -o d.zip
gh pr view 45 -R wikipathways/sandbox-wp-db --json state,labels
gh api "repos/wikipathways/sandbox-wp-db/commits?path=.github/workflows/pr_label_dispatcher.yml" \
  --jq '.[] | "\(.commit.author.date) \(.sha[0:8])"'

# Which job fails in workflow 1, and why
for id in 29390758046 29020074976 28753283643 28753193790; do
  gh run view $id -R wikipathways/sandbox-wp-db \
    --json jobs --jq '[.jobs[] | select(.conclusion=="failure") | .name] | join(",")'
done
gh api repos/wikipathways/sandbox-wp-db/actions/runs/28753193790/logs > w1.zip  # PR 58, two GPMLs
gh pr view 58 -R wikipathways/sandbox-wp-db --json files --jq '[.files[].path]'

# The -eq bug does not abort under errexit
bash -c 'set -e; N=$(echo _drafts/WP0__PR54.md | sed -E "s/WP([0-9]+)__PR.*/\1/");
         if [ "$N" -eq 0 ]; then W=999; else W=$((N)); fi; echo "WP$W"; echo "exit=$?"'

# gh has no --add-body
gh pr edit --help

# Draft artifact layout
gh api repos/wikipathways/sandbox-wp.gh.io/contents/_drafts --jq '.[].name'
gh api repos/wikipathways/sandbox-wp.gh.io/contents/_data/drafts --jq '.[].name'
gh api repos/wikipathways/sandbox-wp.gh.io/contents/draft_assets/WP0__PR10 --jq '.[].name'

# The GPML Version attribute, draft versus published
gh api repos/wikipathways/sandbox-wp.gh.io/contents/draft_assets/WP0__PR10/WP0__PR10.gpml \
  --jq .content | base64 -d | head -3
gh api repos/wikipathways/sandbox-wp-db/contents/pathways/WP1/WP1.gpml \
  --jq .content | base64 -d | head -3

# All three repos public; our permissions on each
for r in sandbox-wp-db sandbox-wp.gh.io sandbox-wp-assets; do
  gh api repos/wikipathways/$r --jq '.name + " private=" + (.private|tostring)'
  gh api repos/wikipathways/$r --jq .permissions
done
curl -s -o /dev/null -w '%{http_code}\n' \
  https://raw.githubusercontent.com/wikipathways/sandbox-wp.gh.io/main/_drafts/WP0__PR10.md
```
