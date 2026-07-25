# Deployment — Strato VHP4Safety cluster (issue #5)

Follows the cluster conventions (image → GHCR so both nodes can pull, `core` overlay network,
GlusterFS-backed data, **no node pinning**, secrets as Docker secrets, Traefik-routed). The
authoritative cluster docs live at `/mnt/gluster/documentation/` on `tgx1` — read `AGENTS.md`
and `operations/infrastructure-guide.md` before deploying; this file is the service-specific
recipe.

> Status: image + CI + swarm recipe authored here; **not yet deployed** (no live verification).

## Image (CI → GHCR)

`.github/workflows/docker-publish.yml` builds on every push to `main` and publishes:

```
ghcr.io/marvinm2/wikipathways-submit:latest
ghcr.io/marvinm2/wikipathways-submit:<sha>
```

Make the GHCR package **public** once (so the cluster pulls without auth), or deploy with
`--with-registry-auth`. `.github/workflows/ci.yml` runs ruff + pytest on every push/PR.

## Datastore

PostgreSQL (SQLite is dev-only). Either run Postgres as its own cluster service with data on
`/mnt/gluster/docker/wikipathways-submit-db/data`, or point `WPSUBMIT_DATABASE_URL` at an
existing managed Postgres. The container entrypoint runs `alembic upgrade head` before serving
whenever the URL is Postgres (see `docs/migrations.md`).

## Secrets (Docker secrets, never in the repo)

```bash
printf '%s' "$(python -c 'import secrets;print(secrets.token_urlsafe(32))')" \
  | docker secret create wpsubmit_session_secret -
printf '%s' "$(python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')" \
  | docker secret create wpsubmit_token_encryption_key -
docker secret create wpsubmit_oauth_client_secret ./oauth_client_secret.txt
docker secret create wpsubmit_webhook_secret       ./webhook_secret.txt
docker secret create wpsubmit_app_key              ./wikipathways-submit-bot.private-key.pem
docker secret create wpsubmit_database_url         ./database_url.txt   # postgresql+psycopg://...
```

The entrypoint hydrates `WPSUBMIT_SESSION_SECRET`, `WPSUBMIT_GITHUB_OAUTH_CLIENT_SECRET`,
`WPSUBMIT_GITHUB_WEBHOOK_SECRET`, `WPSUBMIT_TOKEN_ENCRYPTION_KEY`, and `WPSUBMIT_DATABASE_URL`
from `/run/secrets/*`. The App private key stays a **path** (`WPSUBMIT_GITHUB_APP_PRIVATE_KEY_PATH`).

## Deploy

```bash
mkdir -p /mnt/gluster/docker/wikipathways-submit/data   # only if using SQLite/scratch; Postgres holds state

docker service create \
  --name wikipathways-submit \
  --network core \
  --replicas 1 \
  --secret wpsubmit_session_secret \
  --secret wpsubmit_token_encryption_key \
  --secret wpsubmit_oauth_client_secret \
  --secret wpsubmit_webhook_secret \
  --secret wpsubmit_app_key \
  --secret wpsubmit_database_url \
  --env WPSUBMIT_CONTENT_REPO=wikipathways/wikipathways-database \
  --env WPSUBMIT_SESSION_HTTPS_ONLY=true \
  --env WPSUBMIT_OAUTH_REDIRECT_URI=https://wikipathways-submit.cloud.vhp4safety.nl/auth/callback \
  --env WPSUBMIT_GITHUB_OAUTH_CLIENT_ID=<oauth-app-client-id> \
  --env WPSUBMIT_GITHUB_APP_ID=<app-id> \
  --env WPSUBMIT_GITHUB_APP_INSTALLATION_ID=<installation-id> \
  --env WPSUBMIT_GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/wpsubmit_app_key \
  --env WPSUBMIT_CURATOR_TEAM=wikipathways/curators \
  --label traefik.enable=true \
  --label "traefik.http.routers.wikipathways-submit.rule=Host(\`wikipathways-submit.cloud.vhp4safety.nl\`)" \
  --label traefik.http.routers.wikipathways-submit.entrypoints=websecure \
  --label traefik.http.routers.wikipathways-submit.tls=true \
  --label traefik.http.routers.wikipathways-submit.tls.certresolver=letsencrypt \
  --label traefik.http.services.wikipathways-submit.loadbalancer.server.port=8000 \
  --label traefik.docker.network=core \
  --restart-condition on-failure \
  --with-registry-auth \
  ghcr.io/marvinm2/wikipathways-submit:latest
```

Then request the DNS A record `wikipathways-submit.cloud.vhp4safety.nl → 81.169.246.233` (TGX1;
TGX2 is the manual failover target — Strato allows one A record per subdomain).

## Verify / update

```bash
docker service ls --filter name=wikipathways-submit
docker service logs -f wikipathways-submit
curl -sI https://wikipathways-submit.cloud.vhp4safety.nl/health
docker service update --image ghcr.io/marvinm2/wikipathways-submit:latest wikipathways-submit
```

## Reminders

- **No node pinning** — the image is on GHCR and state is in Postgres, so the task can schedule on
  either node.
- The GitHub App must be installed on the content repo with contents RW, pull_requests RW,
  issues RW, and (for the curator team) org Members:read — see `docs/github-app-setup.md`.
- Register the OAuth App callback + the App webhook URL against the deployed host.
- Update the cluster's `services/service-registry.md` and add `services/wikipathways-submit.md`
  after the first real deploy.
