# naapp

The naapp runtime (`naapp-lib`, the `aweb-naapp` package) and the apps built on
it: Secrets with its runner and values codec (`naapp/secrets`), Logs
(`naapp/logs`), Folio (`naapp/folio`) and Library (`naapp/library`). Each app
keeps its own package, image, version and release.

The layout is the one these trees had in aweb, so the relative paths between
them still resolve. The apps' design documents, the vectors their tests read
and their own build scripts sit at their aweb paths too (`docs/`,
`test-vectors/`, `scripts/`, `candidate-gate/`).

## Where the history comes from

Branch `aweb-import` is the output of `scripts/extract-from-aweb.sh`: every aweb
commit that touched the paths listed in the script, rewritten to hold only those
paths. The current import is from aweb
`d8ee9ed31ad275da7cab3a3cade0006e06587f02`.

This repository is the only home of the runtime and the apps: the extraction
ran once, at the cutover, and nothing more is imported from aweb. The script
records how `aweb-import` was made, and reproduces it from that aweb commit.

## Dependencies from aweb

- `awid-service` and the aweb server (`aweb`, used only by tests) are published
  releases from PyPI, pinned by each project's `uv.lock`.
- Tests that run `aw` use the release that `scripts/install-aw.sh` installs,
  checked against its recorded sha256.
- The `test_production_awid` tests run the AWID release production runs, which
  `scripts/test-egress/production-awid` pins. Move that pin when production
  moves.
- The e2e stacks run the published awid and aweb server images, pinned by
  digest in each app's `docker-compose.e2e.yml`.
- Two e2e images are still built from aweb's source on GitHub, because aweb
  publishes no image of them. Each becomes a pinned published image when aweb
  publishes one:
  - Secrets' federation CLI (`naapp/secrets/docker-compose.e2e.yml`, service
    `cli`), at aweb `d8ee9ed31ad275da7cab3a3cade0006e06587f02`;
  - Library's pre-status registry (`naapp/library/docker-compose.e2e.yml`,
    service `awid-pre-status`), at aweb
    `0bc7546de0b5c002e3496ae4807fb1a4e12bd0b4`.

## Checks that hold across the move

`scripts/test_served_manifest_digests.py` checks that each app serves the
manifest bytes it served in aweb. Installations pin an app by its manifest
digest, so a change there needs every installation to re-pin.

`naapp-lib/tests/server_compat` drives the runtime against the real aweb
server this repository pins: discovery, hook resolution and registrations, team
grants and the task stage receipts. Its fixtures are aweb's own server test
helpers, copied verbatim from that release into `aweb_server_tests/` by
`scripts/server_test_support.py` and checked against their recorded sources.
When the aweb pin moves, rerun the script from the new release tag.

The suites run behind the egress guard in `scripts/test-egress/native.py`,
which refuses network access beyond loopback and the reserved test names.

Two journeys that use the apps stay in aweb's gate and run against published
images by digest: the CLI's Library journey and Folio's document-change and
browser-wake acceptance. They test aweb's client, server and channel-core
against a real app.

## Temporary copies, deleted when the contracts package publishes

The contract part of `aweb_naapp` is also in aweb, where the server uses it. It
will be published from aweb as `aweb-naapp-contracts` (import name
`aweb_naapp_contracts`). When that happens, this repository runs the contracts
package's committed rewrite script, pins the published package and deletes the
copies below.

Contract modules, under `naapp-lib/src/aweb_naapp/`:

- `client_identity.py`, `did_key.py`, `key_file.py`, `manifest.py`,
  `manifest_keys.py`
- `egress/` (the whole package)
- `hooks/__init__.py`, `hooks/strictjson.py`, `hooks/owner_act.py`,
  `hooks/owner_act_nonces.py`, `hooks/migrations/`, `hooks/catalog.py`,
  `hooks/catalog-v1.json`, `hooks/envelope.py`, `hooks/delivery.py`
- `runtime/task_requests.py`, apart from `app_request_credential`, which stays
  in the runtime

Their tests, under `naapp-lib/tests/`:

- `test_manifest_did_key.py`, `test_manifest_file.py`, `test_manifest_tools.py`,
  `test_manifest_v2_vectors.py`, `test_manifest_v3.py`
- `egress/test_addresses.py`, `egress/test_url_vectors.py`,
  `egress/test_egress_is_dependency_light.py`
- `runtime/test_task_request_vectors.py`, `runtime/test_prepare_vectors.py`
- `hooks/test_catalog.py`, `hooks/test_packaged_catalog.py`,
  `hooks/test_envelope.py`, `hooks/test_envelope_bound.py`,
  `hooks/test_envelope_vector_payloads.py`, `hooks/test_delivery.py`,
  `hooks/test_owner_act_signing.py`, `hooks/test_owner_act_vector.py`,
  `hooks/test_owner_act_delivery_vector.py`,
  `hooks/owner_act_delivery_vector.py`,
  `hooks/test_team_certificate_vectors.py`,
  `hooks/test_hooks_is_dependency_free.py`, `hooks/test_binding_cases.py`

Vectors that aweb keeps as canonical. This repository's tests read the
published copies instead:

- From the contracts package: `test-vectors/app-manifests/`,
  `test-vectors/app-read-credential-v1.json`, and in `docs/vectors/`:
  `app-request-credential-v1`, `app-task-authorization-v1`, `task-answer-v1`,
  `page-action-prepare-v1`, `hook-catalog-v1`, `hook-event-envelope-v1`,
  `hook-delivery-credential-v2`, `hook-binding-cases-v1`, `owner-act-v1`,
  `owner-act-delivery-v1`, `team-auth-envelope-v2`,
  `team-certificate-verification-v1`, `principal-attestation-v1`,
  `egress-url-v2`, `egress-address-v1`, `page-proposal-v1`, `logs-chain-v1`,
  `logs-export-v1` (Logs regenerates the logs pair and compares it).
- From the awid-service release: `canonical-json-v1`, `message-signing-v1`,
  `signature-point-encoding-v2`, `signature-primitives-v1`.

The other vectors under `docs/vectors/` belong to this repository:
`secrets-runner-protocol-v1`, `secrets-runner-http-v1`,
`app-usage-credential-v1`, `usage-report-v1`, `page-format-v1`,
`decision-display-v1`, `link-request-v1`, `link-capability-v1`,
`human-role-holding-v1` and `passkey-evidence-v1`.
