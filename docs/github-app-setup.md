# GitHub App (bot) setup

The app uses **two GitHub identities on purpose** (scaffolding-plan §3):

- **Per-user OAuth** (`docs/oauth-setup.md`) — pushes the branch and opens the PR *as the
  submitter*, so authorship is real.
- **GitHub App (bot)** — this doc — performs the privileged, cross-cutting actions the user
  token must not: **merging** on curator approval (so it can satisfy branch protection and is
  never tied to one curator's personal token) and posting the **read-only mirror comment** on
  the PR. It is also the stable identity future webhooks (issue #8) authenticate against, and it
  can supply the **WPID-floor read** (issue #3) when no `WPSUBMIT_GITHUB_TOKEN` is set.

Until the App is configured, `POST /api/reviews/{n}/approve` returns **503** (there is no bot to
merge as), and mirror comments are simply skipped (they are best-effort).

> Local-only project: register the App under **your own** GitHub account and install it on your
> **fork** of `wikipathways-database`. Do not register anything under the `wikipathways` org yet.

## 1. Register the GitHub App

GitHub → **Settings → Developer settings → GitHub Apps → New GitHub App**
(<https://github.com/settings/apps>).

| Field | Value |
|---|---|
| GitHub App name | `wikipathways-submit-bot (dev)` |
| Homepage URL | `http://localhost:8000` |
| Webhook — Active | ✔ (see **Webhooks** below) |
| Webhook URL | `https://<host>/webhooks/github` |
| Webhook secret | a random string; set it as `WPSUBMIT_GITHUB_WEBHOOK_SECRET` |

**Repository permissions** (least privilege for what the bot does):

| Permission | Access | Why |
|---|---|---|
| Contents | Read and write | read the tree for the WPID floor; the bot merge writes to `main` |
| Pull requests | Read and write | merge PRs on approval |
| Issues | Read and write | post/update the read-only mirror comment (PR comments are issue comments) |

Create the App, then:

1. **Generate a private key** → downloads a `.pem`. Keep it out of the repo.
2. Note the **App ID** (numeric, on the App's page).
3. **Install** the App (left sidebar → *Install App*) on your fork of `wikipathways-database`,
   then open the installation and copy the **Installation ID** from its URL
   (`.../installations/<INSTALLATION_ID>`).

## 2. Configure the app

Env vars (all `WPSUBMIT_` prefixed — see `app/config.py`). **Prefer the key-file path over an
inline PEM** so the key can be a mounted Docker secret, never an env value in process listings:

```bash
WPSUBMIT_GITHUB_APP_ID=123456
WPSUBMIT_GITHUB_APP_INSTALLATION_ID=78901234
WPSUBMIT_GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/wpsubmit_app_key   # mounted .pem
# or, for quick local testing only, the PEM contents inline:
# WPSUBMIT_GITHUB_APP_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
```

All three of App ID + installation ID + a key (path or inline) must be present, or the bot stays
disabled and approve routes 503.

## Webhooks (issue #8)

So a PR closed or merged **outside** the app still frees the pathway lock and finalises the
WPID, the App sends webhooks to `POST /webhooks/github`:

- **Subscribe to:** *Pull requests* events. On a `pull_request` event with `action: closed`,
  the app releases the lock, promotes the reservation to MERGED (if `merged: true`) or returns
  the WPID to the pool (if closed unmerged), and moves the review to a terminal state. Delivery
  is idempotent (a duplicate, or the webhook for a merge the app itself performed, is a no-op).
- **Secret:** every request is verified with HMAC-SHA256 over the raw body against
  `WPSUBMIT_GITHUB_WEBHOOK_SECRET` (`X-Hub-Signature-256`); a bad/absent signature is rejected
  401, and an unconfigured secret makes the endpoint 503. Store it as a Docker secret.
- Locks and reservations also auto-expire by TTL (`WPSUBMIT_PATHWAY_LOCK_TTL_DAYS` default 3,
  `WPSUBMIT_WPID_RESERVATION_TTL_DAYS` default 14) — the webhook just makes the common case
  prompt instead of waiting for the TTL. Tune the TTLs against real submitter behaviour.

## 3. On the cluster (secret handling)

Per cluster conventions, the private key is a **Docker secret**, never in the repo or an env var:

```bash
docker secret create wpsubmit_app_key ./wikipathways-submit-bot.private-key.pem
# then in the service definition:
#   --secret wpsubmit_app_key
#   -e WPSUBMIT_GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/wpsubmit_app_key
```

## How the code authenticates (two-legged)

`app/auth/github_app.py`:

```
sign RS256 JWT with the App private key   (iss = App ID, exp ≤ 10 min)
      │
      ▼
POST /app/installations/{id}/access_tokens  (Bearer <JWT>)  → installation token (~1h)
      │  cached until ~60s before expiry
      ▼
HttpGitHubClient(installation_token)  ── merge_pull_request / upsert_issue_comment
```

`get_bot_client` (strict, used by approve → 503 if unconfigured) and `get_bot_optional` (used by
the mirror-comment path → skipped if unconfigured) both build the client from
`app.state.bot_app`. The installation token is minted per request and cached on the `GitHubApp`
object across its short life.
