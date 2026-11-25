#!/usr/bin/env bash
# Builds an app's service image as the release does (its Dockerfile, the
# repository root as the build context), starts it with a minimal
# configuration against the PostgreSQL that IMAGE_SMOKE_DATABASE_URL names, and
# requires /live to answer with the app's id and the build commit in
# IMAGE_SMOKE_SHA. The e2e stacks run the apps from the checkout, so this is
# the check that the image itself imports and serves.
set -euo pipefail

app=${1:?usage: image-smoke.sh <app>}
sha=${IMAGE_SMOKE_SHA:?IMAGE_SMOKE_SHA names the build commit}
database_url=${IMAGE_SMOKE_DATABASE_URL:?IMAGE_SMOKE_DATABASE_URL names the PostgreSQL to start against}
prefix=$(printf '%s' "$app" | tr '[:lower:]' '[:upper:]')
image="naapp-image-smoke-$app"
container="$image-$$"
keys=$(mktemp -d)

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    docker logs "$container" 2>&1 | tail -n 60 || true
  fi
  docker rm -f "$container" >/dev/null 2>&1 || true
  rm -rf "$keys"
  exit "$status"
}
trap cleanup EXIT

docker build --file "naapp/$app/Dockerfile" --tag "$image" .

# The kids the app's manifest declares; throwaway seeds, readable by the
# image's user and writable by no one else (the key-file reader's rule).
case "$app" in
  folio)
    printf '%064d' 1 | tr 0 1 > "$keys/emit.hex"
    printf '%064d' 2 | tr 0 2 > "$keys/grant.hex"
    key_files='{"folio:emit-1": "/keys/emit.hex", "folio:grant-read-1": "/keys/grant.hex"}'
    ;;
  *)
    echo "image-smoke.sh: no smoke configuration for $app" >&2
    exit 2
    ;;
esac
chmod 0755 "$keys"
chmod 0644 "$keys"/*.hex

docker run --detach --name "$container" --network host \
  --volume "$keys:/keys:ro" \
  --env "${prefix}_DATABASE_URL=$database_url" \
  --env "${prefix}_APP_KEY_FILES=$key_files" \
  --env "${prefix}_GIT_SHA=$sha" \
  "$image" >/dev/null

for _ in $(seq 60); do
  if live=$(curl --silent --fail --max-time 2 http://127.0.0.1:8765/live); then
    break
  fi
  if [ "$(docker inspect --format '{{.State.Running}}' "$container")" != true ]; then
    echo "the $app image exited before it served /live" >&2
    exit 1
  fi
  sleep 1
done
[ -n "${live:-}" ] || { echo "the $app image did not serve /live within 60 s" >&2; exit 1; }
python3 - "$app" "$sha" "$live" <<'PY'
import json, sys
app, sha, body = sys.argv[1:]
live = json.loads(body)
assert live.get("service") == app, live
assert live.get("build") == {"git_sha": sha}, live
print(f"the {app} image serves /live as {app} at {sha}")
PY
