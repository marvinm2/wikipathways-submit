# MVP-1 fork test checklist

This tree drops straight into a **fork** of `wikipathways/wikipathways-database`. The three
files sit at their final paths — copy the tree in, don't rearrange. Nothing here touches the
`wikipathways` org until you decide to open the upstream PR.

> These files are generated copies of `../mvp1/`. If you change one, re-copy from `mvp1/`
> (the source of truth) rather than editing both.

```
.github/workflows/pr-preview.yml          # build + validate on pull_request (no secrets, fork-safe)
.github/workflows/pr-preview-comment.yml  # post sticky comment on workflow_run (base-repo, write token)
scripts/validate_pathway.py               # the validation report (stdlib only)
```

## Setup

- [ ] Fork `wikipathways/wikipathways-database` to `marvinm2/wikipathways-database` (or a scratch copy).
- [ ] Copy this tree into the fork root; commit to `main` on the fork.
- [ ] In the fork: **Settings → Actions → General → Workflow permissions** → allow read/write
      (the comment workflow needs `pull-requests: write`; on your own fork you control this).
- [ ] Confirm Actions are enabled on the fork.

## Test A — new pathway (expect a clean-ish preview)

- [ ] Branch off the fork's `main`; add `pathways/WP<big>/WP<big>.gpml` (pick an unused high WPID).
- [ ] Open a PR **within the fork** (fork `main` ← branch).
- [ ] **PR preview** run: green, produces a `pr-preview` artifact containing
      `WP<big>/WP<big>.svg`, `-datanodes.tsv`, `-bibliography.tsv`, `validation.md`.
- [ ] **PR preview comment** run: posts one sticky comment with the validation table.
- [ ] Re-push a change → the **same** comment updates in place (not a second comment).

## Test B — edit an existing pathway (the stale-file fix)

- [ ] Branch; modify an existing `pathways/WP*/WP*.gpml` (e.g. add an unmapped datanode).
- [ ] Open PR. The datanode table + render reflect the **new** content, not the committed one.
- [ ] (Optional) Confirm the fix for finding D: if metadata generation fails, the table shows
      FAIL/missing rather than the old committed table — the derived files are deleted before regen.

## Test C — the hardening (should NOT explode)

- [ ] **Injection (finding A):** add a file literally named
      `pathways/WP<n>/$(touch owned).gpml`. Expect: the run treats it as a filename (no command
      runs; `owned` never appears in the workspace). This is awkward to create via git UI —
      only test if convenient.
- [ ] **Delete-only PR (finding H):** open a PR that only deletes a `WP*.gpml`. Expect: preview
      run succeeds with a "No renderable GPML changes" comment; the comment workflow does **not**
      error on a missing artifact.
- [ ] **Batch PR (finding E):** touch many pathways (if you have a bulk change handy). Expect: if
      the body would exceed ~60 KB it falls back to a one-line-per-pathway roll-up and still posts.

## What each failure looks like

| Symptom | Likely cause | Where to look |
|---|---|---|
| Preview run red at "Generate review artifacts" | meta-data-action jar URL / Java 11 / BridgeDb download | run log, that step's `::group::` |
| Comment run red at "Post or update sticky comment" | head-SHA mismatch (finding B guard) or API perms | github-script step log |
| No comment, comment run green | delete-only PR (expected) or artifact had no `comment.md` | download step + artifact contents |
| `validation.md` shows FAIL "Metadata generated" for a valid pathway | organism not extracted / meta-data-action failed | the pathway's `::group::`, `organism:` line |

## PinPath before/after render (issue #11 — EXPERIMENTAL, validate here)

The `Setup R (PinPath)` → `Install PinPath` → `Render before/after with PinPath` steps add a
PinPath render of both the PR-head version (`WP<id>-after.svg`) and the base-branch version
(`WP<id>-before.svg`, updates only) to the `pr-preview` artifact. The app prefers `-after.svg`
over the gpmlconverter `WP<id>.svg` and shows the two frames side by side. **Not yet run on a
real runner — validate:**

- [ ] `Rscript scripts/render_pinpath.R <a.gpml> out out.svg` produces a non-empty SVG (a *plain*
      render needs only PinPath + imports — no `org.Hs.eg.db`/Bioconductor annotation DB).
- [ ] Confirm `BiocManager::install("PinPath")` resolves on the runner and the **R-packages cache**
      makes subsequent runs fast (first run is minutes). If it's too heavy for every PR, split the
      PinPath steps into their own workflow that uploads into the same `pr-preview` artifact name.
- [ ] For an **update** PR: `git show <base>:<path>` renders a "before"; for a **new** pathway there
      is no base version, so only `-after.svg` appears and the app shows a single frame.
- [ ] Confirm the app picks up the artifact: `GET /previews/{pr}/{before,after}.svg` returns the
      SVGs (needs the bot identity configured for Actions read — see `docs/github-app-setup.md`).
- [ ] Compare PinPath vs `gpmlconverter` render fidelity and decide which is the default "after".

## Validated on a real runner (2026-07-25/26, marvinm2/wikipathways-database)

Ran the pipeline on a WP554 edit PR (fork PR #3). What we learned + fixed:

- ✅ **Plumbing works end-to-end:** `pull_request` → `pr-preview.yml` → `pr-preview` artifact →
  `pr-preview-comment.yml` sticky comment. The validation correctly reported
  `❌ Rendered SVG — render step produced no SVG` when the render failed (the pipeline's job).
- ✅ **gpmlconverter renders without the assets-repo SSH keys** (open question resolved).
- 🔧 **BridgeDb `.bridge` must be downloaded, not just cached.** `configGenerator.sh` only writes
  `gdb.config`; without `installDependencies.sh` the derby is missing →
  `NoSuchFileException Hs_Derby_*.bridge` → meta-data-action fails → no datanodes → no render.
  **Fixed:** added the "Install BridgeDb files (cache miss)" step. Metadata now generates.
- ⚠️ **gpmlconverter's SVG render is blocked by upstream HTTP 400s** — it calls an external
  per-node service that floods `Server responded with status code 400`. Not fixable from here;
  it's an upstream gpmlconverter/service issue. **PinPath (local R render, no Chrome) sidesteps it.**
- 🔧 **PinPath install** needed a sequence of fixes, applied in order across runs: (1) install from
  GitHub (`SyNUM-lab/PinPath`), not Bioconductor release; (2) install `remotes` first; (3)
  authenticate the GitHub API with `GITHUB_PAT: ${{ github.token }}` (unauthenticated `remotes` hit
  the 60-req/hr limit: `cannot open URL .../DESCRIPTION`); (4) **use current-release R, not a 4.4.x
  pin.** An earlier `4.4.2` pin (chosen to avoid devel Bioc 3.23) failed `R CMD build` with
  `ERROR: this R is version 4.4.2, package 'PinPath' requires R >= 4.6.0` — PinPath's `devel` branch
  (v0.99.4, its only branch, no release tag) hard-requires **R ≥ 4.6.0**. Bumped to
  `r-version: "release"` (R 4.6.x now → the *released* Bioc 3.23, paired with R 4.6 since April
  2026, so the devel-Bioc concern is moot). Cache key bumped to `r-pinpath-...-rrelease-v3`.
  - ✅ **The `GITHUB_PAT` fix is confirmed working** (run 30214373998, 2026-07-26): the rate-limit
    error is gone; PinPath downloaded and all its Bioconductor deps (AnnotationDbi, ggraph,
    BiocFileCache, …) resolved + installed. The install now reaches `R CMD build` and only failed on
    the R-version requirement above — which fix (4) addresses.
  - ⏳ **The R-release fix is authored but not yet confirmed green on a runner.** After pushing the
    fix to the fork's `main`, rapid PR close/reopen/push churn appears to have throttled GitHub's
    scheduling of the `pull_request` `pr-preview.yml` (runs stopped registering while the native
    `pull_request_target` workflow kept firing). Re-validate with **one** calm push of any gpml
    change to a PR branch, then confirm PinPath compiles + `WP<id>-after.svg`/`-before.svg` are
    non-empty in the `pr-preview` artifact. (These PinPath steps live only in `fork-staging`, not
    `mvp1/` — they stay experimental here until this render is confirmed, then backport to `mvp1`.)

## Before proposing upstream

- [ ] Confirm the **meta-data-action v1.1.4** invocation matches the live `metadata` job (mirrored,
      but the local-run script pins the older v1.1.2 — verify on a real run).
- [ ] Confirm `gpmlconverter` renders on the fork runner **without** the assets-repo SSH keys
      (it should — generation is local; only pushing needs keys).
- [ ] Decide artifact retention (currently 14 days) and WARN-vs-FAIL severities with curators.
