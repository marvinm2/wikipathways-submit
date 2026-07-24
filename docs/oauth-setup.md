# GitHub OAuth setup

The app acts **as the logged-in user** for writes (branch push, PR open, merge on approval),
so it needs a GitHub **OAuth App**. This is the one step that must be done in a GitHub account —
the code is already in place (`app/auth/`, the `/auth/*` routes). Until it's configured, the
write endpoints return **401** (not logged in) and `/auth/login` returns **503**.

> Local-only project: register the OAuth App under **your own** GitHub account for testing. Do
> not register anything under the `wikipathways` org (buy-in pending).

## 1. Register the OAuth App

GitHub → **Settings → Developer settings → OAuth Apps → New OAuth App**
(<https://github.com/settings/developers>). Fill in:

| Field | Value (local dev) |
|---|---|
| Application name | `wikipathways-submit (dev)` |
| Homepage URL | `http://localhost:8000` |
| Authorization callback URL | `http://localhost:8000/auth/callback` |

Create it, then **Generate a new client secret**. Note the **Client ID** and **Client secret**.

The callback URL must match `WPSUBMIT_OAUTH_REDIRECT_URI` exactly. For a deployed instance use
that host instead, e.g. `https://<host>/auth/callback`, and register a second OAuth App (or
update this one's callback) for it.

## 2. Configure the app

All settings are env vars with the `WPSUBMIT_` prefix (see `app/config.py`). Put them in a
`.env` file (gitignored) or the environment:

```bash
WPSUBMIT_GITHUB_OAUTH_CLIENT_ID=Iv1.xxxxxxxx
WPSUBMIT_GITHUB_OAUTH_CLIENT_SECRET=xxxxxxxxxxxxxxxx
WPSUBMIT_OAUTH_REDIRECT_URI=http://localhost:8000/auth/callback
WPSUBMIT_SESSION_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(32))")

# Who may approve-that-merges (JSON list). Add your own GitHub login to test curation.
WPSUBMIT_CURATORS='["your-github-login"]'

# Optional: a token for reading the live WPID floor (tree ∪ open PRs). Without it the app
# uses WPSUBMIT_DEV_WPID_FLOOR for local dev.
# WPSUBMIT_GITHUB_TOKEN=ghp_xxx
# WPSUBMIT_CONTENT_REPO=your-fork/wikipathways-database
```

- **`OAUTH_SCOPE`** defaults to `public_repo read:user`. `public_repo` is enough to push
  branches and open/merge PRs on a **public** repo (a fork of `wikipathways-database`). Use
  `repo` only if you target a private repo.
- **`SESSION_SECRET`** signs the cookie that holds the user token — must be set to a random
  value in any real deployment; the default is intentionally insecure.

## 3. Run and log in

```bash
.venv/bin/uvicorn app.main:app --reload
```

1. Open <http://localhost:8000/auth/login> → redirects to GitHub → authorize.
2. GitHub returns to `/auth/callback`; the app stores your token + login in the session.
3. Check <http://localhost:8000/auth/me> → `{"authenticated": true, "login": "...", "is_curator": ...}`.
4. `POST /api/submit` / `/api/pathways/{wpid}/update` / `/api/reviews/{n}/approve` now act as you.

## Flow (what the code does)

```
/auth/login  ──302──▶ github.com/login/oauth/authorize?client_id&redirect_uri&scope&state
                        (state stored in the session as a CSRF guard)
GitHub ──302──▶ /auth/callback?code&state
   • verify state matches the session
   • POST github.com/login/oauth/access_token  (code + client secret) → access_token
   • GET  api.github.com/user                   (Bearer token)          → login
   • store {gh_token, gh_login} in the signed session cookie
```

`get_current_user` reads `gh_login` from the session (identity is never a form field a caller
could spoof); `get_github_client` builds an `HttpGitHubClient` from `gh_token`.

## Security notes / later hardening

- The user token currently lives in the **signed session cookie**. It is signed (tamper-proof)
  and `httponly`, but it is still client-held. For production, consider server-side sessions or
  encrypting the token at rest; and set `https_only=True` on the session middleware behind TLS.
- The design (scaffolding-plan §3) also calls for a **GitHub App (bot)** identity for privileged
  cross-cutting actions — merging to satisfy branch protection, posting the preview comment.
  Right now the **curator's own OAuth token** performs the merge (works if the curator has write
  access). Adding the GitHub App is the natural next hardening step and decouples merge/comment
  from any individual's token.
