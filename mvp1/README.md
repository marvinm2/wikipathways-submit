# MVP-1 — Reviewable PRs (staging)

This directory stages **MVP-1** of the [design proposal](../docs/design-proposal.md): make
WikiPathways pull requests reviewable *before* merge. It is the highest-leverage, smallest
piece — it needs **no app and no new repo**, only two workflows + one script added to
`wikipathways/wikipathways-database`.

> **Why staged here, not opened as a PR:** access is *pending team buy-in*. Nothing in this
> folder has been pushed to the `wikipathways` org. When approved, the files ship to the
> content repo at the paths below and this folder can be deleted.

## What ships where

| File here | Ships to (in `wikipathways-database`) |
|---|---|
| `pr-preview.yml` | `.github/workflows/pr-preview.yml` |
| `pr-preview-comment.yml` | `.github/workflows/pr-preview-comment.yml` |
| `validate_pathway.py` | `scripts/validate_pathway.py` |

## Grounding findings (from reading the live pipeline)

`on_gpml_change.yml` in `wikipathways-database` runs **9 jobs** on *push to `main`* — i.e. only
after merge. That is the root cause of "reviewers approve unreadable XML." Mapping the jobs to
what a curator actually needs to review:

| Job | Produces | In review subset? |
|---|---|---|
| `changed-gpmls` | list of changed GPMLs (via `git diff`) | logic reused (adapted to PR base..head) |
| `metadata` | `-info.json`, `-datanodes.tsv` (Java `meta-data-action` + BridgeDb) | ✅ datanodes table |
| `pubmed` | `-bibliography.tsv` (Node `generate-references`) | ✅ references |
| `json-svg` | `.svg`, `.json`, `-thumb.png` (Node `gpmlconverter`, puppeteer) | ✅ **the render** |
| `frontmatter` | `.md` | ➖ optional, not a review artifact |
| `author-list` | author profiles → jekyll repo | ❌ side-repo push |
| `homology-conversion` | homolog GPMLs → homology repo | ❌ side-repo push |
| `sync-site-repo-*` | site content → jekyll repo | ❌ side-repo push |

Key discoveries that shaped the design:

1. **A ready-made push-free runner already exists.** `scripts/local-run/on-gpml-change_local.sh`
   reproduces the whole pipeline locally with **no git pushes** — the `pr-preview.yml`
   generate step reuses its exact generator invocations (cleanup → metadata → pubmed → render).
2. **Dependency chain, not independent jobs.** The render/tables depend on `metadata` running
   first (it produces the identifiers the render annotates and the datanodes table). A preview
   can't run "render only" — it runs metadata → pubmed → render, then validates.
3. **The validation report is the one genuinely new piece** (design §4.4). Nothing upstream
   emits it, so `validate_pathway.py` is new code. Everything else is re-pointing existing
   generators. It checks: empty `<bp:ID>`, datanodes missing an identifier, missing
   ontology-tags/description/authors, reference count, and whether the SVG rendered. It is
   **informational, never a merge gate** — a curator decides.
4. **Cost is bounded.** `configGenerator.sh` downloads BridgeDb `.bridge` files only for the
   organism(s) of the *changed* pathway (not all 36), and the preview runs only the review
   subset, both cached. This addresses the proposal's "preview cost/time" open risk (§9).

## The security split (the one non-obvious architectural choice)

MVP-1 must work on **today's raw PRs, which come from forks**. GitHub gives a fork PR's
workflow **no secrets and a read-only token** — and this pipeline *executes untrusted GPML*
through Java/Node converters. So:

- **`pr-preview.yml`** (`on: pull_request`) — runs in the fork context, no secrets, read-only,
  generates everything, uploads a `pr-preview` artifact. It **cannot** comment. It never
  commits derived files and never pushes to the sister repos.
- **`pr-preview-comment.yml`** (`on: workflow_run`) — runs in the **base-repo** context with
  `pull-requests: write`, downloads the artifact, and posts one **sticky** comment. It never
  checks out or runs PR code — it only reads `comment.md` + `pr-number.txt`.

⚠️ **Do not "simplify" this into a single `pull_request_target` job that checks out PR head.**
That runs untrusted code with a write token — a well-known GitHub Actions vulnerability. The
two-workflow split is deliberate.

## Before/after render (PinPath, #11 / #6)

`pr-preview.yml` also carries an optional **PinPath** (`drawGPML`) render step that produces
`WP<id>-after.svg` (PR head) and `WP<id>-before.svg` (base version, updates only) for the app's
before/after viewer — see `../fork-staging/scripts/render_pinpath.R`. It is best-effort
(`continue-on-error`): if PinPath can't install or render, the run stays green.

**Validated on a real runner** (2026-07-26, fork PR #4, WP554). Two things it needs, learned there:
the GitHub install must set `GITHUB_PAT: ${{ github.token }}` (else `remotes` hits the anon
rate limit), and R must be `release` (PinPath v0.99.x requires **R ≥ 4.6.0**). Because
gpmlconverter's own SVG render is blocked by upstream HTTP-400s, PinPath runs **before** the
validation step and `validate_pathway.py --rendered` accepts multiple candidates in priority
order — so PinPath's `-after.svg` satisfies the "Rendered SVG" check and the overall status is a
genuine PASS.

## Adversarial review — applied hardening

This bundle was run through a multi-agent adversarial review (13 verified findings). The fixes
are already in these files:

- **Script injection (high):** changed filenames reach the shell only via `env:`, never spliced
  into a `run:` script through `${{ }}`.
- **Comment spoofing (high):** the commenter treats the artifact's `pr-number.txt` as untrusted
  and refuses unless the claimed PR's head SHA equals the `workflow_run` head SHA — a poisoned
  artifact cannot redirect the write-token comment onto another PR/issue.
- **Stale-file masking (medium):** committed derived files are deleted before regeneration, so a
  swallowed generator failure shows a visible FAIL instead of last-merged tables as "fresh".
- **Also fixed:** robust organism extraction (not a line-anchored `sed`); comment size cap with a
  compact roll-up; artifact always uploaded (delete-only PRs don't break the commenter);
  per-organism bridge cache key; `generate-references` hoisted out of the per-file loop;
  validator hardened against non-object JSON and oversized TSV fields (`csv.Error`).

One finding was **not** actioned (coverage-denominator, PLAUSIBLE/low): its suggested fix would
false-WARN on healthy pathways where the GPML `<DataNode>` count legitimately exceeds the
tabulated rows (e.g. WP5636 is 96 vs 77).

To test on a fork, use the ready-to-drop tree in [`../fork-staging/`](../fork-staging/CHECKLIST.md).

## How to test (on a fork, before proposing upstream)

1. Fork `wikipathways/wikipathways-database` (or use a scratch copy).
2. Add the three files at the paths in the table above.
3. Open a PR that edits one `pathways/WP*/WP*.gpml`.
4. Expect: the **PR preview** run produces a `pr-preview` artifact (render + tables +
   `validation.md`); the **PR preview comment** run posts/updates the sticky summary comment.
5. The `validate_pathway.py` logic is unit-testable standalone (stdlib only):
   `python validate_pathway.py WP5636 --pathways-dir pathways` against a checked-out pathway.

## Open items before upstreaming

- ✅ Confirmed on a real run (2026-07-26): `meta-data-action` **v1.1.4** invocation, and that
  `gpmlconverter` generates locally without the assets-repo SSH keys. (gpmlconverter's *SVG*
  render is separately blocked by upstream HTTP-400s — PinPath covers the render, see above.)
- Decide artifact **retention** (currently 14 days) and whether to also attach the `.json`.
- Tune which checks are `WARN` vs `FAIL` with curators — severity is a policy choice.
- MVP-1 posts the mirror comment; **approval state stays with GitHub** until the app's
  dashboard (MVP-4) exists to own it.
