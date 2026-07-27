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

| File | What it is |
|---|---|
| `pathway_new.gpml` | A new pathway — *Insulin signaling (demo)*, three gene nodes (INS → INSR → AKT1). No WPID; the app assigns the next real one. |
| `pathway_update.gpml` | The **same** pathway, edited — adds FOXO1, an AKT1 → FOXO1 arrow, and a Glucose metabolite. Use this in the update step. |
| `serve_demo.py` | Launches the app wired to real GitHub (or the offline fake). |

## Run it

```bash
.venv/bin/python demo/serve_demo.py
```

It prints the mode, your login, and the target repo, then serves on `127.0.0.1:8000`. Open
**http://127.0.0.1:8000/demo/login** to sign in (as you) and land on the submit page. Ctrl-C to stop.

Environment overrides: `WPSUBMIT_DEMO_TOKEN` (token, default `gh auth token`), `WPSUBMIT_DEMO_USER`
(login, default `gh api user`), `WPSUBMIT_DEMO_REPO` (default `marvinm2/wikipathways-database`),
`WPSUBMIT_DEMO_FAKE=1` (offline in-memory mode).

## Walk the lifecycle

The app assigns the next free WPID over the fork's tree. At the time of writing that is **WP5641**;
it will differ once you merge (each merged pathway advances the floor). Substitute the WPID you
actually get below.

### 1. Submit the new pathway

1. **Step 1 · Validate**: choose `demo/pathway_new.gpml`, click **Validate & preview**.
2. **Step 2 · Submit**: optionally add a note for reviewers, then **Submit new pathway**.
3. You get a WPID (e.g. **WP5641**) and a link to a **real PR** on the fork. The uploaded file has no WPID — the app assigns it and lays it out at `pathways/WP5641/WP5641.gpml`.

### 2. Review and merge it

1. Go to **/dashboard**. The submission is in the queue with its rendered **after** preview.
2. Expand the card, mark every **required** checklist item (the dotted ones) as **Pass**. **Approve & merge** stays disabled until they all pass.
3. Click **Approve & merge**. This performs a real merge of the PR into the fork's `main`, so `WP5641.gpml` now lives on the fork.

### 3. Update the same pathway

1. Back on **/**, use the **Update an existing pathway** form.
2. Enter the WPID (e.g. **5641**), choose `demo/pathway_update.gpml`, add a "what changed" note, and **Submit update**.
3. You get a second real PR. Its dashboard preview shows a real **before / after**: the merged three-node version vs. your five-node revision.

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
