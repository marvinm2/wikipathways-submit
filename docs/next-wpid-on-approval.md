# Next: assign the WPID at approval, not at submission

Marvin's direction, 2026-07-27. Nothing below is built yet — this is the note to start from.

## The three decisions

1. **A new pathway always gets its own branch, named from the submitter and a placeholder WPID**
   (he said "username and WP1"), not from an id the app picked.
2. **The real WPID is assigned only when the submission is approved.** Until then the submission
   carries the placeholder.
3. **We move to [`wikipathways/sandbox-wp-db`](https://github.com/wikipathways/sandbox-wp-db)** as
   the target repo, instead of the personal fork of `wikipathways-database`.

## Why this is a good change

The atomic allocator exists because ids were being claimed by in-flight PRs that had not merged, so
two submissions could take the same number (the audit found WP5637-5641 handed out several times
over). Assigning at merge removes the race by construction: nothing holds an id while it waits for
review, so nothing can collide. The whole reservation machinery — TTLs, expiry, release-on-failure,
"burn no WPID on a failed submission" — is answering a question that stops being asked.

It also fixes a smaller wart we hit today: a leftover `submit/WP5644` branch from a closed PR
blocked the next submission, because the branch name encoded the id.

## What it changes in this app

- `app/wpid/allocator.py` — allocation moves from `SubmissionService.submit_new_pathway` to
  `CurationService.approve_and_merge`. The `wpid_reservation` table, its TTL, `expire_stale`,
  `release`, and the collision-retry loop in `app/submit/service.py` may all reduce to a single
  "take the next free id inside the merge transaction". Keep the primary-key trick: it is what
  makes concurrent approvals safe.
- `app/wpid/github_floor.py` — still needed at approval time (tree ∪ open PRs ∪ app branches).
  The open-PR and branch terms may become unnecessary once no PR claims an id; check before
  deleting, since power users can still open raw PRs by hand.
- `app/submit/gpml.py` — `assign_wpid` gets called at approval instead of upload, and the file has
  to be **renamed** from the placeholder path to `pathways/WP<id>/WP<id>.gpml` as part of the
  merge. That is a second commit on the branch before merging, or a post-merge commit on `main`.
- `app/submit/service.py` — branch naming, and the `NoPendingSubmission` / revise path, which
  currently finds the open submission by `submit/WP<id>`.
- `app/main.py` — `GET /api/pathways/{wpid}` (`state: pending_new`) keys off the assigned id, so
  the "is this WPID an open submission?" lookup needs another handle.
- The preview, checklist and review rows are keyed by PR number, so they are mostly unaffected.

## Settled

- **The placeholder is `WP0001`.** Marvin confirmed, and it matches the sandbox's own `WP0001`
  branch and its "TEST Create WP0001.gpml" PR. `pathways/WP1/` is a real pathway, so `WP1` was
  never available as the placeholder path.
- **Branch uniqueness comes from a timestamp.** The same person can submit twice, so the branch
  name carries the submitter, the placeholder and a stamp. Proposed exactly:

      WP0001_<username>_<YYYYMMDD-HHMMSS>      e.g. WP0001_marvinm2_20260727-173500

  Id-then-user matches the sandbox's existing `WP4846_egonw`; the readable stamp beats the
  PathVisio plugin's `contribution-1784092149904` when a curator is scanning a branch list. Swap
  in epoch-ms if we would rather be identical to the plugin.
- **Updates keep `update/WP<id>`.** They target a pathway that already has an id, and the
  check-out lock already guarantees one open edit at a time, so nothing is gained by a stamp.

## Open questions, in the order they block work

1. **Who does the rename at approval** — the app (commit the renamed file on the branch, then
   merge) or a workflow in the content repo (after merge)? The sandbox already has
   `3a_approved_pull_request.yml`, dispatched by an `accepted` **label**
   (`pr_label_dispatcher.yml`), which is a natural seam: the app could apply the label and let the
   repo's own pipeline finish the job. Note the file also has to move directory
   (`pathways/WP0001/WP0001.gpml` → `pathways/WP<id>/WP<id>.gpml`), not just change its `Version`.
2. **How does this app relate to the sandbox's existing pipeline?** The sandbox is not a bare
   content repo — it has `1_on_pull_request.yml` → `2_after_pr_processed.yml` →
   `3a_approved` / `3b_rejected`, pushing outputs to `sandbox-wp.gh.io` and `sandbox-wp-assets`.
   Our MVP-1 `pr-preview.yml` overlaps with `1_on_pull_request.yml`. Decide whether we ship the
   preview workflow there at all, or lean on theirs and read its outputs.
3. **What replaces the `pending_new` lookup?** `GET /api/pathways/{wpid}` answers "is this WPID an
   open submission?" so the update form can route a re-upload to the revise path. With no id until
   approval there is nothing to look up by; the submitter needs to find their own open submission
   some other way (their dashboard row, or the PR number).
4. **Do we still need the lock for updates?** Yes — that is about concurrent edits to an existing
   pathway, unaffected by when ids are assigned.

## What the sandbox repo looks like (checked 2026-07-27)

Public, `main`, described as "Playground for new GPML processing and curation". Same layout as
`wikipathways-database` (`pathways/`, `scripts/`, `annotations/`, `communities/`, `downstream/`),
with the full pathway tree. Workflows: `1_on_pull_request.yml`, `2_after_pr_processed.yml`,
`3a_approved_pull_request.yml`, `3b_rejected_pull_request.yml`, `pr_label_dispatcher.yml`,
`on_gpml_change.yml`, plus the two scheduled ones. Recent PRs come from a PathVisio GitHub plugin
(@traybug23) on `contribution-<epoch-ms>` branches, one GPML per PR — the same "one pathway per
pull request" rule we assume.

Also worth knowing: `1_on_pull_request.yml` triggers on `pull_request_target`, which runs in the
base-repo context with a write token. Our MVP-1 workflow deliberately splits that into an
untrusted `pull_request` job plus a `workflow_run` commenter. If we end up contributing there,
that difference is worth raising rather than silently mirroring.

## Where to start

Read this file, then `docs/design-proposal.md` §4.1-4.2 (submission flow and the allocator), then
`app/submit/service.py` and `app/wpid/allocator.py`.

The naming is settled, so the submission half can be built without further input: branch
`WP0001_<username>_<stamp>`, file at `pathways/WP0001/WP0001.gpml`, no id reserved. Question 1
(who renames at approval) is what blocks the approval half, and it is worth answering by reading
`3a_approved_pull_request.yml` in the sandbox first — if that pipeline already does the publishing
work after an `accepted` label, our approve step may be smaller than it looks.
