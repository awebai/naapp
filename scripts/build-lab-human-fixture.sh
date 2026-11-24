#!/usr/bin/env bash
# Build Secrets' lab human fixture against the aw release this repository pins.
#
# Usage: scripts/build-lab-human-fixture.sh <directory>   builds <directory>/lab-human-fixture
#
# The fixture uses aw's Go packages, so it builds in a module that requires the
# aw release scripts/install-aw.sh installs. It needs the network for Go modules.
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <directory>" >&2
  exit 2
fi
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
destination=$1
version=$("$root/scripts/install-aw.sh" --version)
module=$(mktemp -d)
trap 'rm -rf "${module:?}"' EXIT

/bin/cp -f "$root/naapp/secrets/tests/testdata/lab_human_fixture.go" "$module/main.go"
mkdir -p "$destination"
(
  cd "$module"
  go mod init labhumanfixture >/dev/null
  go get "github.com/awebai/aw@v$version" >/dev/null
  go mod tidy >/dev/null
  GOMAXPROCS=2 go build -o "$destination/lab-human-fixture" .
)
