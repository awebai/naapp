# Shipping naapp artifacts

`main` remains the reviewed synchronization branch and never publishes by
itself. Each artifact is released by an immutable tag on one exact tested
commit.

| Tag | Published artifact |
|---|---|
| `secrets-vX.Y.Z` | `ghcr.io/awebai/secrets` image, and PyPI `aweb-naapp-journey-support` (`naapp/secrets/e2e/`) at the same version |
| `logs-vX.Y.Z` | `ghcr.io/awebai/logs` image |
| `folio-vX.Y.Z` | `ghcr.io/awebai/folio` image |
| `library-vX.Y.Z` | `ghcr.io/awebai/library` image |
| `aweb-secrets-runner-vX.Y.Z` | PyPI `aweb-secrets-values`, then `aweb-secrets-runner`, at the same version |
| `aweb-naapp-vX.Y.Z` | PyPI `aweb-naapp` (`naapp-lib/`), then `aweb-naapp-testing` (`naapp-lib/testing/`), at the same version |

Manifest-backed tags must equal the version in their package manifest. A tag
that publishes two packages requires both manifests at the tag version:
`aweb-naapp-journey-support` versions with Secrets, and the runner and values
manifests must have equal versions, with the runner requiring exactly that
values version.

The [customer Secrets/Logs/runner procedure](../naapp/secrets/deploy/RELEASE.md)
uses these same entry points. Neither an image recipe nor these tag mappings
alone qualifies a customer release or a target platform.

## Release candidate spellings

The Python lanes also accept strict `X.Y.ZrcN` versions, with `N >= 1`:
`aweb-secrets-runner-v0.1.0rc1`, for example. The manifest versions, including
the runner's lockstep values package, must equal the tag. The image lanes accept
final versions only.

RCs are optional: a final needs no prior RC. One fully green CI run qualifies
the exact tree that is tagged, and that tree is what publishes. Python
RCs publish to PyPI. On every image lane only a final published by its own tag
push moves `latest`. A `workflow_dispatch` of the image workflow publishes an
existing tag's image again as a recovery (`--recovery`): it publishes and reads
back the version only and reports `latest unchanged (recovery)`. Images are
published by the tag workflow. A direct `publish_release.py` or
`make release-publish` run without `--recovery` behaves like a tag push and
moves `latest` for a final, with no serialization against workflow runs.

Tags are immutable. Run `git fetch --tags` before either entry point.

## 1. Qualify the commit and tag it

Choose the commit and every artifact tag it should publish, then run one
command (`SOURCE` defaults to `HEAD`):

```sh
make release-candidate SOURCE=<sha> TAGS='secrets-v0.2.0 logs-v0.2.0'
```

The commit must be on `origin/main`, every tag must match its manifests at that
commit, and no tag may already exist. The commit qualifies only through a CI
run of exactly that commit (`ci.yml`), read with `gh`: a push to `main` or a
manual run, both of which test every package, completed with every job a
success, the `naapp CI` check among them. A skipped job is a package the run did
not test, so it does not qualify. A push run that waited behind a newer push is
replaced and never runs; qualify that commit with a manual run of it
(`gh workflow run ci.yml --ref <branch at the commit>`). The command then
creates the annotated tags locally on that SHA, each naming its run.

A tag of a PyPI lane (`aweb-naapp-v`, `aweb-secrets-runner-v`, `secrets-v`)
also needs `FILES=<directory>`:

```sh
make release-candidate SOURCE=<sha> TAGS='secrets-v0.2.0' FILES=~/naapp-release-files
```

Every CI run's `release-files` job builds the wheel and sdist of every PyPI
package twice and fails unless both builds are byte for byte the same. It then
installs each lane's wheels, by path, into a clean environment that imports them,
and keeps the files with their `SHA256SUMS` as the run's `release-files`
artifact. The command downloads that artifact from the qualifying run, refuses
unless the files match its `SHA256SUMS`, and copies exactly the tag's lane files
(one wheel and one sdist of each of its packages at the tag version) into
`<directory>/<tag>/`. The annotated tag's message lists each of those files as a
`sha256sum` line, the lowercase sha256, two spaces and the file's basename, so
`git tag -l --format='%(contents)' <tag> | grep -E '^[0-9a-f]{64}  ' | sha256sum -c`
checks a local copy.

`make release-checks` runs the same checks on one host, for example to
reproduce a CI failure.

## 2. Publish the tested tags

Push the tags explicitly, one command at a time:

```sh
git push origin refs/tags/secrets-v0.2.0
git push origin refs/tags/logs-v0.2.0
```

GitHub omits tag-push events when more than three tags are pushed together, so
never batch this step.

A PyPI tag publishes to PyPI only through its GitHub release. After pushing it,
create the release with exactly the files the command kept (it prints the line):

```sh
gh release create secrets-v0.2.0 --verify-tag --notes-from-tag ~/naapp-release-files/secrets-v0.2.0/*
```

Publishing the release starts `.github/workflows/pypi-release.yml`; a tag push
alone uploads nothing to PyPI. The workflow fetches the annotated tag, downloads
every release asset and runs `publish_release.py --only pypi --assets <dir> <tag>`.
That refuses unless the tag is annotated, its message lists files, and the assets
are exactly those files with those sha256 digests, and the lane's own files: a
missing file, an extra file or a different digest refuses before anything is
uploaded. It also installs the lane's wheels together, by path, into a clean
environment that takes every other dependency from PyPI, and imports them: a
lane that needs another lane's release PyPI does not serve yet uploads nothing,
and its workflow can be rerun once that release is out. So release
`aweb-naapp-v` before `secrets-v` (`aweb-naapp-journey-support` requires
`aweb-naapp-testing`) and before an `aweb-secrets-runner-v` whose `aweb-naapp`
floor it raises. It then uploads each package's files in lane order, only those PyPI
does not yet serve, so a rerun after a partial upload adopts what is there, and
reads PyPI back. A file PyPI already serves with other bytes refuses permanently.
The upload uses the repository's `PYPI_TOKEN` secret; no PyPI token lives on the
operator's machine. Nothing in a release builds a Python package again.

Each tag starts only its owning thin publisher. Publishers stage the exact
tagged source (the PyPI publisher, the locked release files), publish or adopt
exact bytes, verify registry readback, and stop. They do not run product suites, move a branch, infer changed artifacts or
create another tag.

A `secrets-v*` tag has both a Python and an image publisher. The image workflow
runs on the tag push and selects `--only oci`; the PyPI workflow runs on the
published release and selects `--only pypi`, so each needs only its own
registry credentials. Both selectors still validate the tagged manifest
versions first.

## Runnerless publication

Every registry-backed tag can publish from the operator machine using the same
tag dispatcher:

```sh
make release-publish TAG=logs-v0.2.0
```

Provide the credential required by the destination:

- GHCR: `GHCR_USERNAME` and `GHCR_TOKEN`

The runnerless path builds images from a detached worktree at the local tag and
refuses a same-version byte conflict. PyPI is not runnerless: only the release
workflow uploads, with the repository's PyPI secret, so a runnerless
`secrets-v` uses `python3 scripts/publish_release.py --only oci <tag>`.

## App images

An image tag publishes its app's image from `naapp/<app>/Dockerfile` and the
repository root, to `<namespace>/<app>`. The namespace is `NAAPP_IMAGE_NAMESPACE`:
the workflow sets it to `ghcr.io/<repository owner>` (`ghcr.io/awebai`) unless the
repository variable of that name gives another GHCR namespace, as a dry run under
throwaway names does. A runnerless publication without it uses `ghcr.io/awebai`. It builds Linux amd64 and arm64 into one OCI archive, checked
before the registry is touched. The exact OCI helper validates the archive and
each platform's version/source labels. Version tags are immutable: a conflicting
digest refuses, an identical one is adopted, and each published index is read
back. The printed `release-index` receipt carries the repository, digest, source
SHA and version. Deployments, and aweb's gate journeys that run Folio and
Library, pin `ghcr.io/awebai/<app>@sha256:...` from that receipt.

An unavailable tag listing remains a refusal; a 403 or 404 is not permission to
assume absence. GHCR's "name unknown" for a repository that does not exist yet
is never taken as absence on its own either: a new repository is published only
by a `workflow_dispatch` whose `first_publication` input names it
(`--first-publication <repository>`), and then only if the listing says exactly
that. A named repository that already lists tags refuses as "not a first
publication"; a tag push never passes the flag. `ghcr.io/awebai/folio` and
`ghcr.io/awebai/library` are first published that way.

Before declaring an image publicly pullable, the coordinator must arrange public
package visibility and verify anonymous retrieval in an environment with no
registry credentials: read the immutable version tag, require its index digest
to equal the publication receipt, and fetch both platform manifests, configs and
layers by digest. Authenticated publication success alone does not complete this
acceptance step.

## Repository boundary

This repository never changes or deploys aweb, aweb-cloud or the servers
repository. They pin this repository's published images by digest and its
packages by version.

There are no release-intent tags, done tags, release branches, workflow
monitors, or cross-repository release transactions.
