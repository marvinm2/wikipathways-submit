# Deployment — Strato VHP4Safety cluster (issue #5)

Follows the cluster conventions (image → GHCR so both nodes can pull, `core` overlay network,
GlusterFS-backed data, **no node pinning**, secrets as Docker secrets, Traefik-routed). The
authoritative cluster docs live at `/mnt/gluster/documentation/` on `tgx1` — read `AGENTS.md`
and `operations/infrastructure-guide.md` before deploying; this file is the service-specific
recipe.

> Status (2026-07-27): the **datastore is deployed and running** on the cluster; the app service
> is not up yet. Approved hostname is `upload.wikipathways.org`. The remaining blocker is DNS —
> see "Hostname and DNS" below.

## Hostname and DNS

The service is `upload.wikipathways.org`, not a `*.cloud.vhp4safety.nl` name. That zone is run by
WikiPathways on Cloudflare, not by Strato, which changes two things:

- **There is no wildcard to rely on.** `*.wikipathways.org` is proxied through Cloudflare to
  GitHub Pages, so an unconfigured name silently answers as a GitHub 404 rather than not
  resolving. Verify with `dig +short upload.wikipathways.org A`: the cluster is reachable only
  when that returns `81.169.246.233`, not Cloudflare addresses (`104.21.x.x` / `172.67.x.x`).
- **The record must be DNS-only (grey cloud).** Traefik issues certificates over HTTP-01, so
  Let's Encrypt has to reach tgx1 directly. Proxied through Cloudflare the challenge cannot
  complete. `sandbox.wikipathways.org` and `classic.wikipathways.org` are already DNS-only in
  that zone and are the precedent to point at.

Per the cluster's `AGENTS.md`, **add the Traefik router labels only after DNS resolves to the
cluster** — a Host rule on a name that does not point here makes the ACME challenge loop. Deploy
the service without them first, then add them with `docker service update`.

Check the origin is reachable before adding the router:

```bash
curl -sI -H "Host: upload.wikipathways.org" http://81.169.246.233/     # expect Traefik's 308
```

## Image (CI → GHCR)

`.github/workflows/docker-publish.yml` builds on every push to `main` and publishes:

```
ghcr.io/marvinm2/wikipathways-submit:latest
ghcr.io/marvinm2/wikipathways-submit:<sha>
```

Make the GHCR package **public** once (so the cluster pulls without auth), or deploy with
`--with-registry-auth`. `.github/workflows/ci.yml` runs ruff + pytest on every push/PR.

## Datastore — deployed

PostgreSQL (SQLite is dev-only). Running as its own swarm service, following the pattern the
cluster's other Postgres services use — a `local` volume bind-mounted onto GlusterFS, and
`stop-first` so two containers never open the same data directory:

```bash
mkdir -p /mnt/gluster/docker/wikipathways-submit-db/data/db_data

docker service create \
  --name wikipathways-submit-db \
  --network core \
  --replicas 1 \
  --update-order stop-first \
  --restart-condition on-failure \
  --secret wpsubmit_db_password \
  --env POSTGRES_USER=wpsubmit \
  --env POSTGRES_DB=wpsubmit \
  --env POSTGRES_PASSWORD_FILE=/run/secrets/wpsubmit_db_password \
  --env PGDATA=/var/lib/postgresql/data/pgdata \
  --mount 'type=volume,source=wikipathways-submit-db-data,target=/var/lib/postgresql/data,volume-driver=local,volume-opt=type=none,volume-opt=o=bind,volume-opt=device=/mnt/gluster/docker/wikipathways-submit-db/data/db_data' \
  postgres:16
```

`PGDATA` points at a subdirectory of the mount because Postgres refuses to initialise into a
directory that is itself a mount point. The app entrypoint runs `alembic upgrade head` before
serving whenever the URL is Postgres (see `docs/migrations.md`).

## Secrets (Docker secrets, never in the repo)

Generate the machine-generated ones **on the node**, so the values never travel:

```bash
umask 077; TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
openssl rand -base64 33 | tr -d '\n/+=' | cut -c1-32 > "$TMP/dbpass"
openssl rand -base64 48 | tr -d '\n'                 > "$TMP/session"
python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode(),end='')" > "$TMP/fernet"
printf 'postgresql+psycopg://wpsubmit:%s@wikipathways-submit-db:5432/wpsubmit' "$(cat "$TMP/dbpass")" > "$TMP/dburl"

docker secret create wpsubmit_db_password           "$TMP/dbpass"
docker secret create wpsubmit_session_secret        "$TMP/session"
docker secret create wpsubmit_token_encryption_key  "$TMP/fernet"
docker secret create wpsubmit_database_url          "$TMP/dburl"
```

Those four exist already. The remaining three come from the GitHub-side registration
(`docs/oauth-setup.md`, `docs/github-app-setup.md`) and still have to be created:

```bash
docker secret create wpsubmit_oauth_client_secret ./oauth_client_secret.txt
docker secret create wpsubmit_webhook_secret      ./webhook_secret.txt
docker secret create wpsubmit_app_key             ./wikipathways-submit-bot.private-key.pem
```

The App private key stays a **path** (`WPSUBMIT_GITHUB_APP_PRIVATE_KEY_PATH`), never an env value.

The entrypoint hydrates `WPSUBMIT_SESSION_SECRET`, `WPSUBMIT_GITHUB_OAUTH_CLIENT_SECRET`,
`WPSUBMIT_GITHUB_WEBHOOK_SECRET`, `WPSUBMIT_TOKEN_ENCRYPTION_KEY`, and `WPSUBMIT_DATABASE_URL`
from `/run/secrets/*`. The App private key stays a **path** (`WPSUBMIT_GITHUB_APP_PRIVATE_KEY_PATH`).

## Deploy — stage 1, no router

Deploy the app first without Traefik labels, so the image, the secrets and the Alembic migration
are all proven against the real Postgres before DNS is in play.

```bash
mkdir -p /mnt/gluster/docker/wikipathways-submit/data   # preview/draft cache

docker service create \
  --name wikipathways-submit \
  --network core \
  --replicas 1 \
  --restart-condition on-failure \
  --secret wpsubmit_session_secret \
  --secret wpsubmit_token_encryption_key \
  --secret wpsubmit_database_url \
  --secret wpsubmit_oauth_client_secret \
  --secret wpsubmit_webhook_secret \
  --secret wpsubmit_app_key \
  --mount type=bind,source=/mnt/gluster/docker/wikipathways-submit/data,target=/data \
  --env WPSUBMIT_CONTENT_REPO=wikipathways/sandbox-wp-db \
  --env WPSUBMIT_PUBLISH_MODE=pipeline \
  --env WPSUBMIT_SUBMIT_IDENTITY=bot \
  --env WPSUBMIT_REQUIRE_PREVIEW_CHECK=false \
  --env WPSUBMIT_APP_BASE_URL=https://upload.wikipathways.org \
  --env WPSUBMIT_OAUTH_REDIRECT_URI=https://upload.wikipathways.org/auth/callback \
  --env WPSUBMIT_SESSION_HTTPS_ONLY=true \
  --env WPSUBMIT_PREVIEW_CACHE_DIR=/data/preview-cache \
  --env WPSUBMIT_GITHUB_OAUTH_CLIENT_ID=<oauth-app-client-id> \
  --env WPSUBMIT_GITHUB_APP_ID=<app-id> \
  --env WPSUBMIT_GITHUB_APP_INSTALLATION_ID=<installation-id> \
  --env WPSUBMIT_GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/wpsubmit_app_key \
  --env WPSUBMIT_CURATOR_TEAM=wikipathways/curators \
  --with-registry-auth \
  ghcr.io/marvinm2/wikipathways-submit:latest
```

`WPSUBMIT_REQUIRE_PREVIEW_CHECK=false` is not optional here. It defaults to true and gates on
`pr-preview.yml`, which does not exist on `sandbox-wp-db` and never will — left on, every
approval returns 409.

### `WPSUBMIT_SITE_NOTICE` — say so when the target cannot publish

A standing banner on every page. Empty (the default) renders nothing.

Set it on any deployment whose target repository cannot actually complete a publication — a
sandbox, or a fork that lacks the sister-repo credentials the publish workflow pushes with. The
submit page tells people the database will publish their pathway and assign its WPID; where that
is not true, this is the only thing that says so.

The prompt for it, on 2026-07-28: a pathway arrived from an unfamiliar account through
`upload.wikipathways.org` while it pointed at a fork where neither the publish workflow nor the
rejection workflow can close a pull request. That one turned out to be a colleague testing, so
nobody lost anything — but from inside the app it was indistinguishable from a real submission,
and had it been real it would have gone nowhere with nothing on screen to say so. That is the
case for the banner: by the time you can tell the two apart, the silent failure has happened.

```bash
docker service update \
  --env-add 'WPSUBMIT_SITE_NOTICE=Sandbox deployment. Submissions open a real pull request but are not published to WikiPathways yet, and no WPID is assigned. Please do not rely on this for work you need published.' \
  wikipathways-submit
```

It is free text rather than something derived from `WPSUBMIT_PUBLISH_MODE`, because whether a
target *can* publish depends on credentials held by other repositories that this app cannot see.

Verify from inside the overlay network, since nothing is routed yet:

```bash
docker service logs wikipathways-submit | grep -i alembic     # migrations ran
docker run --rm --network core curlimages/curl:latest -sS http://wikipathways-submit:8000/health
```

## Deploy — stage 2, add the router once DNS lands

Only after `dig +short upload.wikipathways.org A` returns `81.169.246.233`:

```bash
docker service update \
  --label-add traefik.enable=true \
  --label-add 'traefik.http.routers.wikipathways-submit.rule=Host(`upload.wikipathways.org`)' \
  --label-add traefik.http.routers.wikipathways-submit.entrypoints=websecure \
  --label-add traefik.http.routers.wikipathways-submit.tls=true \
  --label-add traefik.http.routers.wikipathways-submit.tls.certresolver=letsencrypt \
  --label-add traefik.http.services.wikipathways-submit.loadbalancer.server.port=8000 \
  --label-add traefik.docker.network=core \
  wikipathways-submit

curl -sI https://upload.wikipathways.org/health
```

## Update

```bash
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
