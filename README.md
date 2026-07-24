# wikipathways-curator

> **Provisional name** — decide the final repo name before `gh repo create` (candidates:
> `wikipathways-curator`, `wikipathways-submit`, `pathway-portal`).

A hosted web app that is the front door for **submitting and curating WikiPathways pathways**
now that all content lives on GitHub. It lets anyone submit a new pathway or an update without
touching git, opens a real pull request against
[`wikipathways/wikipathways-database`](https://github.com/wikipathways/wikipathways-database),
assigns the WPID, and gives curators a review dashboard with a rendered before/after preview.

**Status:** planning. See [`docs/design-proposal.md`](docs/design-proposal.md) for the full
rationale (grounded in a 3-month PR audit) and [`docs/scaffolding-plan.md`](docs/scaffolding-plan.md)
for the build plan.

## Why

The raw GitHub PR flow serves neither submitters nor curators: submissions arrive malformed
(no WPID, wrong filenames), concurrent GPML edits are unmergeable, WPIDs already collide across
in-flight PRs, and curators are asked to approve unreadable XML because the reviewable artifacts
(rendered SVG, data-node/reference tables) are only generated *after* merge. This app fixes the
altitude problem for both roles.

## What it does

- **Submit** (anyone, GitHub OAuth) — upload GPML → app assigns WPID, names/lays out files,
  opens a PR.
- **Update** (anyone) — check out a pathway (one editor at a time), upload a revision, PR off
  the latest `main`.
- **Preview** — render + validation runs on the PR and is shown in the dashboard and mirrored
  as a PR comment, so review happens on the pathway, not the XML.
- **Curate** (whitelisted ~20 curators) — dashboard with before/after render, checklist, and
  one-click approve-that-merges.

## Boundary

This repo holds the app, dashboard, WPID/lock registry, GitHub App, and deployment. The only
change to `wikipathways-database` is one added Actions workflow that renders + validates on
`pull_request`. The app talks to the content repo purely through the GitHub API.
