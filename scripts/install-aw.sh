#!/usr/bin/env bash
# Install the aw release this repository tests against.
#
# Usage: scripts/install-aw.sh <directory>   installs <directory>/aw
#        scripts/install-aw.sh --version     prints the release version
#
# The archive is the published release asset, checked against the sha256
# recorded here, so a changed asset fails instead of installing.
set -euo pipefail

version=1.36.59

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <directory> | --version" >&2
  exit 2
fi
if [ "$1" = --version ]; then
  echo "$version"
  exit 0
fi
destination=$1

case "$(uname -s)/$(uname -m)" in
  Linux/x86_64) platform=linux_amd64 sha256=d45d91a6a0c5221ae456a5991e3c8d5b8264d41804bdc7ba338df4deb01b8be4 ;;
  Linux/aarch64 | Linux/arm64) platform=linux_arm64 sha256=6102a1dffc12947ca3d8564c6b7acf805d0979dc0d19ab38e578c7e0b8961b48 ;;
  Darwin/arm64) platform=darwin_arm64 sha256=ccae5c87c7e9766fea8e46cb4dc41906fa2f2624a15ee45cb8abcfd865f1c10e ;;
  Darwin/x86_64) platform=darwin_amd64 sha256=a54906cd298a24932245f45a90efef18d7af077e34251edf92bc22ee6945ce17 ;;
  *)
    echo "no aw $version release for $(uname -s)/$(uname -m)" >&2
    exit 1
    ;;
esac

archive=aw_${version}_${platform}.tar.gz
work=$(mktemp -d)
trap 'rm -rf "${work:?}"' EXIT

curl -fsSL --retry 3 -o "$work/$archive" \
  "https://github.com/awebai/aw/releases/download/v$version/$archive"
actual=$(openssl dgst -sha256 -r "$work/$archive" | cut -d ' ' -f 1)
if [ "$actual" != "$sha256" ]; then
  echo "$archive has sha256 $actual, expected $sha256" >&2
  exit 1
fi
tar -xzf "$work/$archive" -C "$work" aw
mkdir -p "$destination"
install -m 0755 "$work/aw" "$destination/aw"
