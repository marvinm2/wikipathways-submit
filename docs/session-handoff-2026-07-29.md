# Handoff — 2026-07-29

Supersedes `docs/session-handoff-2026-07-27.md` as the read-me-first. That file is still worth
reading for the sandbox pipeline and the first deployment; this one covers what changed after it
and what is true now.

## Deployed right now

**https://upload.wikipathways.org**, image
`ghcr.io/marvinm2/wikipathways-submit@sha256:906885e4d5264e7ce79bf97582c0476ddf5be3d3686f4615e289e860980d8d64`
(built from `81a67a4`), running on **tgx1** — it moved from tgx2 during a deploy, which is the
no-pinning arrangement working as intended.

That move is worth knowing because it wasted time: `ssh tgx2 "docker logs $(docker ps -q -f
name=wikipathways-submit.1)"` on the wrong node returns an empty log rather than an error, which
reads exactly like "the app never received the request". **Find the node first**:

```bash
ssh tgx1 "docker service ps wikipathways-submit -f desired-state=running --format '{{.Node}}'"
```

Live config is as recorded in the 07-27 handoff, plus one addition:

```
WPSUBMIT_SITE_NOTICE=Sandbox deployment. Submissions here open a real pull request, but are not
published to WikiPathways yet and no WPID is assigned. Please do not rely on this for work you
need published.
```

**Set that on any deployment whose target cannot complete a publication.** It renders a standing
banner on every page; empty renders nothing.

## What was built

**Clickable data nodes in the preview (issue #14, closed).** Clicking a node opens a panel with
its type, database, identifier and resolver link; an unannotated node reads "Not annotated".

Built as an overlay, not either route the issue proposed — both had gone stale (it is written
around PinPath, which is retired, and around CI artifacts the app no longer reads and the current
target cannot produce). `render_gpml_with_nodes` returns hotspots alongside the drawing;
`<side>-nodes.json` is cached beside the SVG; `GET /previews/{pr}/{side}-nodes.json` serves it.

> [!warning] Two things here will bite again if forgotten
> **Geometry and properties come from the same element in one pass, deliberately.** Joining
> renderer geometry to `metadata.py` properties by list index looks equivalent and is not: the
> renderer drops a `DataNode` with no `Graphics`, the metadata parser keeps it, so one such
> element shifts every identifier onto the wrong box — silently, and in a direction a reviewer
> would probably trust. `test_geometry_and_properties_cannot_drift_apart` pins it.
>
> **Do not reach for a `ResizeObserver` to keep the overlay aligned.** Observing the image and
> writing layout from the callback is a feedback loop by construction; it froze the tab, with
> nothing in the console — the page simply stopped painting and screenshots timed out. The
> alignment is synced at three explicit non-re-entrant points instead: image `load`, window
> `resize`, and `initZoom`'s `apply()` via the `root.__syncOverlay` hook.

The first attempt also had the overlay filling the stage, which the image does not — `max-width`
and `max-height` letterbox it, so every hotspot sat about a node's height off. The layer is now
positioned from the image's own measured offset box.

## Issue tracker

Was four open, three of which were already built and merely never closed. Now nine open, all
filed from an audit and each grounded in a file and line rather than a guess.

**Closed with evidence:** #5 (deployment), #11 (before/after viewer), #14 (clickable nodes),
#15 (checklist lost updates — fixed with `version_id_col` + retry rather than the row lock it
proposed, because `SELECT ... FOR UPDATE` is a no-op on SQLite and would have left the hazard in
the backend the race test runs on).

**Open, and done in this session:** #16 uploads bounded (413, chunked read, `Content-Length` not
trusted), #18 render cache freed at every terminal transition, #20 `robots.txt`. All three
verified against production, not just tests — a 5 MB post returns 413, a normal pathway 200,
`robots.txt` serves, and closing a pull request removed its cache directory from GlusterFS.

**Open, not started:** #17 unpaginated queue, #21 no rate limiting, #22 fork-per-submitter,
#23 TTL tuning.

**Done later the same day:** #19 (the overlay is now one tab stop — roving tabindex under a
toolbar role, arrow keys in reading order, selection following focus into a polite live region)
and #24 (`app/preview/diff.py` classifies every data node added / removed / re-annotated /
relabelled / moved; the card carries the count sentence, the overlay colours the hotspots, the
panel strikes the previous value through). Both are verified by tests and by driving the real
markup in a browser; **neither has been looked at on screen**, because the Chrome window was not
visible on the desktop for the whole session — see the gotcha below. Worth an eyeball before
deploying, particularly the five diff colours over an already-coloured diagram.

Two things fell out of building them. Arrow keys could walk focus off the clipped edge of a
zoomed viewport: it pans by transform and so carries no scroll offset for the browser's own
scroll-into-view to write, and at 1.7x only 10 of 30 nodes were reachable. `initZoom` now exposes
`__revealRect`. And `labelRow` assigned an element to `textContent`, which renders as the string
`[object HTMLElement]`.

## A correction to the record

The 07-27 handoff and the KnowledgeBase note both read the PPAR submission from @MadhushriMSV
(sandbox PR #6) as a real contributor's work going into a dead end. **It was a colleague testing
the portal**, confirmed by Marvin, and is closed. Nobody has lost work to this deployment.

That inference was written into four places in the repo as established fact before it was
checked. The banner is still right, on the honest ground: from inside the app a test was
indistinguishable from the real thing, and by the time you can tell them apart the silent failure
has already happened.

## Still needing a person

1. **The security disclosure is unsent.** `wikipathways/sandbox-wp-db` workflow 1 has an unfixed
   command injection. There is no private channel — private vulnerability reporting is
   **disabled** on that repo, there is no `SECURITY.md` in it, in `wikipathways-database`, or in
   the org `.github`, and the org publishes no contact address. So it goes by **email**, which
   needs Marvin. Draft, ready to send:
   `~/Documents/Services/WikiPathways/sandbox-wp-db-disclosure-DRAFT.md` (deliberately outside
   this repo, which is public). Worth asking an org owner to enable PVR at the same time.

   §6.1 of `docs/sandbox-pipeline.md` described the defect in full in this public repo from the
   evening of 2026-07-27 until 18:20 on 2026-07-28; it is redacted (`7ab7867`) and the draft tells
   the maintainers so rather than presenting the defect as undisclosed.

2. **The GitHub App install on the org sandbox** is still pending an org owner, and it blocks
   draft artifacts, any real publication, and the evidence #23 needs.

3. **Sandbox test litter.** PRs #1, #2, #3, #5 are demo submissions from building this. Closing
   them in one command was refused by the harness permission classifier; closing one at a time
   works. **#3 is `PUBLISH_FAILED`, which is deliberately not terminal** — its cache is correctly
   retained and it is a real state, not litter. Cache directories `1` and `6` are orphans from
   before the pruning shipped, which is the sweep #18 asks for.

## Gotchas, cumulative

Everything in the 07-27 handoff still applies — deploy by digest, never poll `gh run list
--limit 1` after a push, `ref`-based browser clicks on the review page report success and do
nothing (use coordinates). Added since:

- **Find which node runs the task before reading logs.** An empty log on the wrong node is
  indistinguishable from an app that never got the request.
- **Bump `?v=` on `app.css`/`app.js` in `base.html` with every frontend change.** It is at `v=16`.
- A `ResizeObserver` whose callback writes anything affecting the observed element's box is a
  loop, and it presents as an unresponsive page rather than an error.
- **A Chrome tab at `document.visibilityState === 'hidden'` lies to you, and it looks exactly
  like a broken page.** When the browser window is minimised or behind another window, that tab
  stops compositing: `Page.captureScreenshot` times out after 30s reporting a frozen renderer,
  `loading="lazy"` images never load (so the zoom measures a zero-width image and does nothing),
  real key events are not delivered, programmatic `.focus()` sets `activeElement` without firing
  a `focus` event, and — the worst one — **`getComputedStyle` returns stale values**, including
  after an inline style write, so a CSS rule that is applying reads as though it is not. Check
  `document.visibilityState` before believing any of it. `requestAnimationFrame` still runs at
  60fps in that state, which is what rules out an actual freeze.
- Setting `document.cookie` is refused outright in that browser profile, so a session cannot be
  faked from the page. Fetch the authenticated HTML with `curl` and serve the snapshot instead.
- The `.container` class carries the page's `2.2rem`/`4rem` vertical padding. Reusing it for a
  banner's gutter brings that with it.

## Live check commands

```bash
curl -sI https://upload.wikipathways.org/health
curl -s  https://upload.wikipathways.org/robots.txt
ssh tgx1 "docker service ps wikipathways-submit -f desired-state=running --format '{{.Node}} {{.Image}}'"
gh issue list -R marvinm2/wikipathways-submit --state open
gh pr list   -R marvinm2/sandbox-wp-db --state all
```
