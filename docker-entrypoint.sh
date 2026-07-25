#!/bin/sh
# Container entrypoint: hydrate Docker secrets, run DB migrations, then start the app.
set -e

# Load a secret file (Swarm mounts secrets at /run/secrets/<name>) into an env var, unless the
# env var is already set. Keeps sensitive values out of the image and the process' `docker inspect`.
_load_secret() {
  eval "current=\$$1"
  if [ -z "$current" ] && [ -f "$2" ]; then
    export "$1=$(cat "$2")"
  fi
}
_load_secret WPSUBMIT_SESSION_SECRET          /run/secrets/wpsubmit_session_secret
_load_secret WPSUBMIT_GITHUB_OAUTH_CLIENT_SECRET /run/secrets/wpsubmit_oauth_client_secret
_load_secret WPSUBMIT_GITHUB_WEBHOOK_SECRET   /run/secrets/wpsubmit_webhook_secret
_load_secret WPSUBMIT_TOKEN_ENCRYPTION_KEY    /run/secrets/wpsubmit_token_encryption_key
_load_secret WPSUBMIT_DATABASE_URL            /run/secrets/wpsubmit_database_url
# The GitHub App private key stays a file path:
#   WPSUBMIT_GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/wpsubmit_app_key

# Apply migrations before serving, but only for a real (non-SQLite) database — SQLite dev uses
# create_all. Idempotent: `upgrade head` on an up-to-date DB is a no-op.
case "${WPSUBMIT_DATABASE_URL:-}" in
  postgresql*|postgres*)
    echo "[entrypoint] alembic upgrade head"
    alembic upgrade head
    ;;
esac

exec "$@"
