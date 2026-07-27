# Labels to create in wikipathways/sandbox-wp-db

The repaired `3a_approved_pull_request.yml` labels the pull request with the outcome of
publication, so the state is visible in the PR list without opening the run log. Neither
label exists in the repository yet (checked against the live label list), and both have to
exist before the workflow uses them: `gh pr edit --add-label` fails on an unknown label.
The workflow tolerates that failure — labelling never blocks a publication — but then the
state is invisible, which defeats the point.

Both colours are already in use in the repository, so nothing new enters the palette:
`0E8A16` is the green of `tests passed`, `B60205` the red of `tests failed`.

| Label | Colour | Description |
|---|---|---|
| `published` | `0E8A16` | The pathway has been published and the PR is closed |
| `publish failed` | `B60205` | Publication failed; see the PR comment for the failing step |

Create them once, with push access to the repository:

```bash
gh label create "published" \
  --repo wikipathways/sandbox-wp-db \
  --color 0E8A16 \
  --description "The pathway has been published and the PR is closed"

gh label create "publish failed" \
  --repo wikipathways/sandbox-wp-db \
  --color B60205 \
  --description "Publication failed; see the PR comment for the failing step"
```

Neither label triggers anything. `pr_label_dispatcher.yml` only reacts to `accepted`,
`rejected` and `resubmitted`, and a label added with `GITHUB_TOKEN` does not start a new
workflow run in any case, so 3A cannot re-trigger itself by labelling.

3A also **removes** `accepted` when a run fails before anything has been pushed, so that
re-applying it starts a new run. That is a removal, not an addition, so it does not
re-fire the dispatcher either.
