#!/usr/bin/env bash
# Extract the history of the naapp runtime and apps from one aweb commit.
#
# Usage: scripts/extract-from-aweb.sh <aweb repository URL or path> <aweb commit> <output directory>
#
# Writes a new repository at <output directory> whose branch `aweb-import`
# holds every aweb commit that touched the extracted paths, rewritten to
# contain only those paths, with authors, dates and messages unchanged.
#
# The rewrite is deterministic for a given path list: extracting a later aweb
# commit reproduces the same rewritten commits for the earlier history and
# appends the new ones, so the naapp repository takes newer aweb history with
# an ordinary merge of `aweb-import`. Changing either path list below rewrites
# every commit that touches the changed paths.
set -euo pipefail
trap 'echo "$0: failed at line $LINENO" >&2' ERR

if [ "$#" -ne 3 ]; then
  echo "usage: $0 <aweb repository URL or path> <aweb commit> <output directory>" >&2
  exit 2
fi
source_repo=$1
source_commit=$2
output=$3

if [ -e "$output" ]; then
  echo "refusing to write into existing $output" >&2
  exit 1
fi

# Paths extracted, exactly as they are laid out in aweb. A path ending in /
# is a directory; any other path is one file.
kept_paths=(
  # The runtime and the apps.
  naapp-lib/
  naapp/secrets/
  naapp/logs/
  naapp/folio/
  naapp/library/
  # The apps' design and reference documents.
  docs/build-a-naapp.md
  docs/design/agent-pages.md
  docs/design/agent-pages/
  docs/design/secrets.md
  docs/design/secrets-build-plan.md
  docs/design/secrets-dispatch-interfaces.md
  docs/page-link-client.md
  docs/page-links.md
  docs/page-renderer.md
  docs/page-store.md
  docs/secrets-job-record.md
  docs/secrets-onepassword-binding.md
  docs/secrets-runner-protocol.md
  # Vectors the runtime and the apps test against.
  docs/vectors/app-request-credential-v1.json
  docs/vectors/app-task-authorization-v1.json
  docs/vectors/app-usage-credential-v1.json
  docs/vectors/canonical-json-v1.json
  docs/vectors/decision-display-v1.json
  docs/vectors/egress-address-v1.json
  docs/vectors/egress-url-v2.json
  docs/vectors/hook-binding-cases-v1.json
  docs/vectors/hook-catalog-v1.json
  docs/vectors/hook-delivery-credential-v2.json
  docs/vectors/hook-event-envelope-v1.json
  docs/vectors/human-role-holding-v1.json
  docs/vectors/link-capability-v1.json
  docs/vectors/link-request-v1.json
  docs/vectors/logs-chain-v1.json
  docs/vectors/logs-export-v1.json
  docs/vectors/message-signing-v1.json
  docs/vectors/owner-act-delivery-v1.json
  docs/vectors/owner-act-v1.json
  docs/vectors/page-action-prepare-v1.json
  docs/vectors/page-format-v1.json
  docs/vectors/page-proposal-v1.json
  docs/vectors/passkey-evidence-v1.json
  docs/vectors/principal-attestation-v1.json
  docs/vectors/secrets-runner-http-v1.json
  docs/vectors/secrets-runner-protocol-v1.json
  docs/vectors/signature-point-encoding-v2.json
  docs/vectors/signature-primitives-v1.json
  docs/vectors/task-answer-v1.json
  docs/vectors/team-auth-envelope-v2.json
  docs/vectors/team-certificate-verification-v1.json
  docs/vectors/usage-report-v1.json
  test-vectors/app-manifests/
  test-vectors/app-read-credential-v1.json
  # The apps' own build, test and release scripts.
  candidate-gate/secrets-shards.json
  scripts/check-naapp-e2e-aw.sh
  scripts/check-naapp-lib-package.py
  scripts/secrets_shards.py
  scripts/test_secrets_shards.py
  # The egress guard the suites run behind.
  scripts/test-egress/denied-reads.json
  scripts/test-egress/native.py
  scripts/test-egress/policy.py
  scripts/test-egress/test_native.py
  # The PostgreSQL client the suites back up and restore with.
  scripts/test-egress/install-pgdg-key.sh
  scripts/test-egress/install-postgres-client.sh
  scripts/test-egress/keys/ACCC4CF8.asc
  scripts/test-egress/keys/README.md
  scripts/test-egress/test_pgdg_key.py
  # The apps' CI workflows.
  .github/workflows/folio-ci.yml
  .github/workflows/library-ci.yml
  .github/workflows/logs-ci.yml
  .github/workflows/secrets-ci.yml
  # The release machinery and its record.
  .github/workflows/pypi-release.yml
  .github/workflows/service-image-release.yml
  docs/release.md
  scripts/fixture_repo.py
  scripts/oci-exact-publish.sh
  scripts/publish_release.py
  scripts/pypi-exact-publish.sh
  scripts/release_candidate.py
  scripts/release_versions.py
  scripts/test_shipping.py
)

# Files under the kept paths that never enter this repository, in any commit:
# the lab's operating hand-offs, which name operator hosts, and the team
# deployment tests, which belong to the servers repository.
dropped_paths=(
  naapp/secrets/deploy/artifacts/linux-amd64-dfa093a39/LAB3-HEALTH-HANDOFF.md
  naapp/secrets/deploy/artifacts/linux-amd64-dfa093a39/LAB3-RUNNER-NATIVE-HANDOFF.md
  naapp/secrets/deploy/artifacts/linux-amd64-dfa093a39/RUNNER-EGRESS-LAUNCH-PROPOSAL.md
  naapp/secrets/tests/test_team_deployment_images.py
  naapp/secrets/tests/test_team_deployment_staging.py
)

# naapp-lib, folio and library entered aweb on 2026-07-29 by subtree merges
# (c97e90af13, da3e336329, 3d2861ee22). Their second parents below are the
# tips of the original repositories, whose commits store files at the root.
# Those commits are moved into the directory the merge placed them in, so the
# history from before the merges is kept.
subtree_tips=(
  "4dbeb4bc08e941f44d91971bb8cbe7448e28a4ed naapp-lib/"
  "44e620f684254d84f8de3d05b1fbf882799cb559 naapp/folio/"
  "833b4de6a9e9f93a3dd3ad0b2201a9658d617a88 naapp/library/"
)

git clone --quiet --no-local --no-checkout "$source_repo" "$output"
cd "$output"
git checkout --quiet -b aweb-import "$source_commit"
resolved=$(git rev-parse HEAD)

# Keep only the extracted branch, so no other ref carries unfiltered history.
git remote remove origin
for ref in $(git for-each-ref --format='%(refname)'); do
  if [ "$ref" != refs/heads/aweb-import ]; then
    git update-ref -d "$ref"
  fi
done

# Every kept path must exist at the source commit.
for path in "${kept_paths[@]}"; do
  git cat-file -e "$resolved:${path%/}" 2>/dev/null || {
    echo "$path does not exist at $resolved" >&2
    exit 1
  }
done

# The tree the result must have: aweb's files under the kept paths, without
# the dropped ones.
expected_listing=$(git rev-parse --git-dir)/expected-listing
git ls-tree -r --full-tree "$resolved" -- "${kept_paths[@]%/}" |
  awk -F '\t' 'NR == FNR { dropped[$0] = 1; next } !($2 in dropped)' \
    <(printf '%s\n' "${dropped_paths[@]}") - > "$expected_listing"

# Python literal mapping each subtree commit to the directory it belongs in.
prefixes="{"
for entry in "${subtree_tips[@]}"; do
  read -r tip prefix <<<"$entry"
  git merge-base --is-ancestor "$tip" HEAD || {
    echo "subtree tip $tip is not an ancestor of $resolved" >&2
    exit 1
  }
  for sha in $(git rev-list "$tip"); do
    prefixes+="b'$sha': b'$prefix', "
  done
done
prefixes+="}"
kept_directories=""
kept_files=""
for p in "${kept_paths[@]}"; do
  if [[ $p == */ ]]; then
    kept_directories+="b'$p', "
  else
    kept_files+="b'$p', "
  fi
done
dropped=$(printf "b'%s', " "${dropped_paths[@]}")

git filter-repo --force --quiet --refs aweb-import --commit-callback "
prefix = $prefixes.get(commit.original_id)
kept_directories = ($kept_directories)
kept_files = {$kept_files}
dropped = {$dropped}
kept = []
for change in commit.file_changes:
    if prefix is not None and change.filename is not None:
        change.filename = prefix + change.filename
    if change.type == b'DELETEALL':
        kept.append(change)
        continue
    name = change.filename
    if name in dropped:
        continue
    if name in kept_files or name.startswith(kept_directories):
        kept.append(change)
commit.file_changes = kept
"

git reset --quiet --hard aweb-import
git gc --quiet --prune=now

if ! diff -u "$expected_listing" <(git ls-tree -r --full-tree HEAD) >&2; then
  echo "the extracted tree differs from aweb $resolved under the kept paths" >&2
  exit 1
fi
for path in "${dropped_paths[@]}"; do
  if [ -n "$(git log --all --format=%H -- "$path")" ]; then
    echo "dropped path $path is still in history" >&2
    exit 1
  fi
done

echo "extracted aweb $resolved into $output (branch aweb-import at $(git rev-parse HEAD), $(git rev-list --count HEAD) commits)"
