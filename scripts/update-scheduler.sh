#!/usr/bin/env bash
#
# update-scheduler.sh — rebuild the scheduler image from the local source and
# restart the container. Run from anywhere; it operates on the repo it lives in.
#
# Overrides (optional):
#   CONTAINER_NAME  default "kptncook-scheduler"
#   IMAGE_NAME      default "kptncook-scheduler"
#   ENV_FILE        host .env mounted at /data/.env (default /root/kptncook/.env)
#   DATA_VOLUME     named volume for /data (recipes/state); unset = ephemeral

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

NAME="${CONTAINER_NAME:-kptncook-scheduler}"
IMAGE="${IMAGE_NAME:-kptncook-scheduler}"
ENV_FILE="${ENV_FILE:-/root/kptncook/.env}"

cd "$REPO_ROOT"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: env file not found: $ENV_FILE (set ENV_FILE=...)" >&2
    exit 1
fi

echo "==> Removing existing container (if any)"
docker stop "$NAME" 2>/dev/null || true
docker rm "$NAME" 2>/dev/null || true

echo "==> Building $IMAGE from Dockerfile.scheduler"
docker build -f Dockerfile.scheduler -t "$IMAGE" .

echo "==> Starting $NAME"
run_args=(-d --name "$NAME" --restart unless-stopped -v "$ENV_FILE:/data/.env:ro")
[[ -n "${DATA_VOLUME:-}" ]] && run_args+=(-v "$DATA_VOLUME:/data")
docker run "${run_args[@]}" "$IMAGE"

echo "==> Done. Follow logs with: docker logs -f $NAME"
docker ps --filter "name=$NAME"
