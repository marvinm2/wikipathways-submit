# Demo: submit a new pathway, then update it

A walkthrough of the full lifecycle against **your real fork**
(`marvinm2/wikipathways-database`), authenticated as **you** via your GitHub CLI token. It opens
**real** pull requests and, on approve, performs a **real** merge into the fork's `main`. No
OAuth App or GitHub App registration is needed — the launcher injects your token directly, so the
identity and every API call are real.

An offline, in-memory fallback is available for a dry run (`WPSUBMIT_DEMO_FAKE=1`), which touches
no network and no repo.

## Prerequisites

- The dev env is set up (see the top-level README: `uv venv ... && uv pip install -e ".[dev]"`).
- `gh auth status` shows you logged in as `marvinm2` with `repo` scope (used to open/merge PRs).
- Push access to the fork (you have admin on `marvinm2/wikipathways-database`).

## Files

Three GPML files tell one story (*Insulin signaling (demo)*), each staged to show off a feature:

| File | Role in the story | What the checklist does |
|---|---|---|
| `pathway_new.gpml` | **Submit** — INS → INSR → IRS1, but **IRS1 has no identifier** and there are no references. | "Data nodes annotated" auto-**fails** (names IRS1); references N/A. A curator requests changes. |
| `pathway_revised.gpml` | **Revise** — same pathway with IRS1 now annotated **and a reference added**. Re-upload it on the Update tab; it commits onto the same PR. | Data nodes auto-**pass**; "References resolve" becomes a **pending** check with a clickable identifiers.org link. |
| `pathway_update.gpml` | **Update** — after merge, extends the pathway with AKT1, a Glucose metabolite, and a second reference (title unchanged). | Data nodes / references relevant; "Title and description meaningful" is auto-**N/A** (unchanged → scoped out). |
| `serve_demo.py` | Launches the app wired to real GitHub (or the offline fake). | |

## Run it

```bash
.venv/bin/python demo/serve_demo.py
```

It prints the mode, your login, and the target repo, then serves on `127.0.0.1:8000`. Open
**http://127.0.0.1:8000/demo/login** to sign in (as you) and land on the submit page. Ctrl-C to stop.

Environment overrides: `WPSUBMIT_DEMO_TOKEN` (token, default `gh auth token`), `WPSUBMIT_DEMO_USER`
(login, default `gh api user`), `WPSUBMIT_DEMO_REPO` (default `marvinm2/wikipathways-database`),
`WPSUBMIT_DEMO_FAKE=1` (offline in-memory mode), `WPSUBMIT_DEMO_CURATOR=0` (below).

### Seeing the portal as someone who is not a curator

By default the demo puts you on the curator whitelist, so you only ever see the reviewer's half of
the app. `WPSUBMIT_DEMO_CURATOR=0` puts a whitelist in place that you are *not* on:

```bash
WPSUBMIT_DEMO_CURATOR=0 WPSUBMIT_DEMO_FAKE=1 .venv/bin/python demo/serve_demo.py
```

The banner then prints `role : NOT a curator`. Submit a pathway and open its review: the automated
checks, the diagram, the diff and the metadata tables all still render, the checklist is visible but
has no Pass/Fail/N/A chips, and the assign, request-changes, reject and approve controls are gone —
replaced by one sentence saying only curators can decide. The revision upload stays, because you are
still the submitter. Combine it with `WPSUBMIT_DEMO_USER=someone-else` to be neither.

Worth knowing when you use this: that explanatory sentence is only rendered while the review is
still open or has changes requested. On a review that has been approved, rejected or published, a
non-curator sees a card with no controls and no explanation of why.

## Walk the lifecycle

The app assigns the next free WPID over the fork's tree. At the time of writing that is **WP5641**;
it will differ once you merge (each merged pathway advances the floor). Substitute the WPID you
actually get below.

### 1. Submit the new pathway (with a deliberate gap)

1. **New pathway** tab: choose `demo/pathway_new.gpml`, optionally add a note, **Submit new pathway**.
2. You get a WPID (substitute yours below) and a **real PR** on the fork, laid out at `pathways/WP<id>/WP<id>.gpml`.

### 2. As a curator, request changes

1. Go to **/dashboard** and expand the card. The checklist is pre-filled: **"Data nodes annotated" auto-fails**, naming out-of-the-box passes, references show N/A.
2. Click **Request changes**, note *"IRS1 has no identifier and there's no reference — please annotate it and add a citation"*, **Send request**. The review moves to the **Changes requested** filter and the note is posted on the PR.

### 3. As the submitter, revise

1. **Update existing** tab: type the WPID — the status line reads *"pending new submission… Submitting revises it"*.
2. Choose `demo/pathway_revised.gpml`, **Submit revision**. It commits onto the **same PR** and re-opens the review.
3. Back on the dashboard: data nodes now **pass**, and **"References resolve" is a pending check** — the References panel shows the paper as a clickable identifiers.org link.

### 4. Approve and merge

1. Mark the remaining human checks (render, description, references) as **Pass**, then **Approve & merge** (allowed once the `pr-preview` CI is green). `WP<id>.gpml` now lives on the fork's `main`.

### 5. Update the merged pathway

1. **Update existing** tab: the same WPID now reads *"…found. Submitting opens an update."*
2. Choose `demo/pathway_update.gpml`, tick some **"What changed?"** boxes, **Submit update**.
3. A second PR opens with a real **before/after** render. Its checklist shows scoping in action: data nodes and references are relevant, but **"Title and description meaningful" is auto-N/A** because they didn't change.

## Clean up the fork afterwards

The demo leaves real artifacts. To reset the fork:

```bash
REPO=marvinm2/wikipathways-database
WP=5641   # the WPID you were assigned

# close the still-open update PR and delete its branch
gh pr close <update-pr-number> --repo $REPO --delete-branch

# delete the (merged) submission branch
gh api -X DELETE repos/$REPO/git/refs/heads/submit/WP$WP 2>/dev/null || true

# the approve step merged the new pathway onto main; remove that file to make the fork pristine
gh api -X DELETE repos/$REPO/contents/pathways/WP$WP/WP$WP.gpml \
  -f message="Remove demo pathway WP$WP" \
  -f sha="$(gh api repos/$REPO/contents/pathways/WP$WP/WP$WP.gpml --jq .sha)" \
  -f branch=main
```

(Or just ask me and I'll run the cleanup.)

## What this demonstrates

- App-owned naming: the upload carries no WPID; the app assigns the next free one over the real tree and lays the file out deterministically.
- The submitter note flows into the PR body (**Submitter note** on the new pathway, **What changed** on the update).
- The check-out lock + branch-off-latest-`main` update flow, and the in-app before/after render.
- Approve-that-merges gated to the curator checklist, merging a real PR.

## Notes / limits

- The registry (WPID reservations, locks, reviews) is a throwaway SQLite file in a temp dir; restarting the server resets it. The **GitHub** side is real and persists on the fork until you clean it up.
- Guard rails to try: upload a non-GPML file in Step 1 (rejected before any WPID is spent, and before any GitHub call); or re-upload the update before its PR merges — because the pathway is still checked out to you, it reuses the same update PR instead of opening a second one.
- For a no-side-effects dry run, use `WPSUBMIT_DEMO_FAKE=1` — same UI and flow, entirely in-memory.
- This launcher is a local demo harness; it is not the deployed app and is not wired to any production repository.
