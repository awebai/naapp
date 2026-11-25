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
| `aweb-naapp-vX.Y.Z` | Closed: aweb publishes `aweb-naapp` and `aweb-naapp-testing` until the cutover |

Manifest-backed tags must equal the version in their package manifest. A tag
that publishes two packages requires both manifests at the tag version:
`aweb-naapp-journey-support` versions with Secrets, and the runner and values
manifests must have equal versions, with the runner requiring exactly that
values version.

The `aweb-naapp-v` lane exists, with its two packages, but the candidate and the
publisher both refuse its tags while aweb still publishes `aweb-naapp`. It opens
at the cutover, when `CLOSED_LANES` in `scripts/release_versions.py` drops it.

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

Each tag starts only its owning thin publisher. Publishers stage the exact
tagged source, publish or adopt exact bytes, verify registry readback, and
stop. They do not run product suites, move a branch, infer changed artifacts or
create another tag.

A `secrets-v*` tag dispatches both the Python and image publishers. The PyPI
workflow selects `--only pypi`; the image workflow selects `--only oci`, so each
needs only its own registry credentials. Runnerless publication without `--only`
publishes both. Both selectors still validate the tagged manifest versions
before checking out that exact commit.

## Runnerless publication

Every registry-backed tag can publish from the operator machine using the same
tag dispatcher:

```sh
make release-publish TAG=logs-v0.2.0
make release-publish TAG=aweb-secrets-runner-v0.1.0
```

Provide the credential required by the destination:

- PyPI: `UV_PUBLISH_TOKEN`
- GHCR: `GHCR_USERNAME` and `GHCR_TOKEN`

The runnerless path builds from a detached worktree at the local tag and
refuses a same-version byte conflict.

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
