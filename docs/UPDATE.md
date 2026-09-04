# Update, migration and rollback model

This document defines the release-to-update contract for penczREQ 0.5.2.
Production deployment is never automatic and requires explicit operator
approval for the concrete operation.

Fresh install is the primary route for new users because 0.5.2 is the planned
first public release. Upgrade `0.4.3 -> 0.5.2` is retained as the legacy
migration path and as the reference versioned-migrator contract; 0.4.3, 0.5.0
and 0.5.1 were not public releases.

## TrueNAS target and API contract

The installer consumes the TrueNAS 25.10 API namespace and its technical
preflight currently accepts the TrueNAS SCALE 25.10.x branch. The empirically
validated appliance target is **TrueNAS SCALE 25.10.6**; fresh install, private
upgrade, rollback, and the ZFS dataset contract passed for the 0.5.2 candidate.

## Image tags and promotion

Every approved release produces an immutable version tag:

```text
ghcr.io/<owner>/penczreq:0.5.2
```

After that exact image passes tests, hardened-image inspection, SBOM generation
and vulnerability gates, the release workflow may promote it to:

```text
ghcr.io/<owner>/penczreq:stable
```

The numbered tag is the rollback anchor. `stable` is only a convenience pointer
to the latest approved release; it is never updated by a push to `main`.
Publishing is triggered only by a published GitHub Release, runs in the protected
`release` environment and requires a selected repository `LICENSE`.

The workflow separates verification from publication. The verification job has
read-only repository access, disables persisted checkout credentials and rejects
a release commit that is not contained in `origin/main`. It seals the tested image
and evidence into one short-lived workflow artifact. A dependent job in the
protected `release` environment is the only job with `contents: write` and
`packages: write`; it verifies that artifact before pushing the immutable image,
then `stable`, and finally the fixed release-asset list.

## Simple image-only update

An image-only update is eligible only when all of the following remain
compatible:

- Compose service shape and entry points;
- published ports and access modes;
- mounts, ownership and writable paths;
- required environment variables and secrets;
- Public/Control database schema and shared configuration contract;
- backup and rollback semantics.

The operator still records the currently running digest, validates both
databases and creates fresh paired backups before changing the image reference.
The update must target an approved numbered tag or its verified digest, not an
unreviewed moving tag.

## Versioned migrator

A versioned migrator is mandatory if any of these change:

- database schema, migration order or cross-database assumptions;
- Compose, service names, ports, mounts, datasets or permissions;
- secret names, formats, encryption keys or bootstrap contract;
- access-mode, trusted-proxy or forwarded-header contract;
- TrueNAS Custom App metadata or portal definitions.

The migrator must be idempotent where practical, fail closed, preserve existing
secrets during upgrade and write a private machine-readable result. A dry-run
must describe planned actions with `mutations_performed: false` and must not
create real credentials or call TrueNAS mutation methods.

The 0.5.2 schema includes nullable `requests.title_en`, so `0.4.3 -> 0.5.2` is a
versioned-migrator update and is not eligible for the simple image-only path.
In the official upgrade flow, the installer first validates and backs up both
databases and records the ZFS rollback snapshot, then performs `app.update`.
TrueNAS preserves the stopped state when updating an existing stopped Custom
App, so the installer inspects the state, starts only an explicit `STOPPED` app
with the `app.start` job, and waits for `RUNNING`. Other non-running states are
only polled with a bounded timeout; no parallel start is issued. Control then
starts first and
`Database.initialize()` applies the idempotent local `ALTER TABLE`; Public
starts only after Control is healthy. The installer checks both databases again
and refuses a successful result unless `requests.title_en` exists. Fresh 0.5.2
databases contain the nullable column directly in the canonical schema.

The separate private `0.5.0/0.5.1 -> 0.5.2` UAT exercises the same installer
upgrade path. It verifies that existing session and encryption secrets and all
manual/legacy trusted-proxy entries are preserved, while `public.env` and
`control.env` are repaired to `root:root` with mode `0600`. When their content
is already current, ownership repair changes only metadata. Runtime-owned
`app/.vapid-private.pem` remains owned by the application user with mode `0600`.

For each TrueNAS service configured in reverse-proxy mode, the generated
environment enables runtime gateway resolution. Before Uvicorn configures
forwarded headers, the service derives a unique directly connected RFC1918 IPv4
default gateway, trusts only its `/32`, and merges it with explicit manual
entries. The value is not persisted as a user setting, so a container restart
after Docker network recreation replaces the previous automatic gateway.
Ambiguous, malformed, off-link or non-private route data fails closed without
expanding trust. LAN mode and generic Compose remain opted out by default.

This local schema migration does not contact TMDB or rewrite existing titles.
English title enrichment is a separate, controlled operator action:

```text
python -m request_app.cli backfill-english-titles
python -m request_app.cli backfill-english-titles --apply
```

The first command performs TMDB reads and reports the plan without database
mutations. `--apply` fetches every planned title before one atomic database
write phase. The backfill is not an automatic or mandatory part of the schema
upgrade: records with `title_en IS NULL` remain valid and EN rendering falls
back to the original title. Run enrichment only against a backed-up private
copy during migration validation. Production mode refuses the network backfill
unless the separate production override is supplied after an explicit
approval; it is not part of ordinary application startup or request-list
rendering.

TMDB `404` entries are counted as skipped and retain the original-title
fallback. Authentication, transport, timeout and other HTTP failures abort the
operation before the title-write transaction.

## Required pre-update evidence

Before a production update:

1. identify the current app version, Compose and image digest;
2. run SQLite `quick_check` and `foreign_key_check` on both databases;
3. create a transactionally consistent backup pair and verify it;
4. create a recursive ZFS snapshot for the application root dataset;
5. record snapshot, backup paths, previous image and previous Compose in
   `ROLLBACK.json`;
6. verify disk space and retain the previous numbered image;
7. obtain explicit approval for the exact production action.

## Rollback

Stop after any failed health or integrity check. Rollback restores one coherent
application state:

1. stop the Custom App;
2. restore the recorded recursive snapshot or the matching pair of databases;
3. restore the previous Compose and numbered image/digest;
4. start Public and Control;
5. run database checks, authentication, list rendering, TMDB and Web Push smoke
   tests.

Never restore only one database after a change involving accounts, settings,
security state or encryption. Exact production commands must be derived from the
real `ROLLBACK.json` and approved separately.

## Release evidence contract

When it runs on a capable, approved release runner, the gated workflow is
configured to produce or attach:

- deterministic installer archive and SHA-256 checksum;
- release notes, release manifest and `SHA256SUMS`;
- SPDX JSON image SBOM after building the image;
- dependency, secret, source/config and image vulnerability reports;
- publication privacy-gate report;
- the pushed immutable image digest.

Audit and scan failures remain release failures. Reports are uploaded even when
a gate fails so the reason is reviewable; they do not turn a red result green.

Local Windows 11 container validation has now executed real Linux/amd64 image
builds, a no-cache rebuild, hardened-image Public/Control runtime inspection,
an SPDX image SBOM and Trivy source/config/image scans. The functional baseline
remains the frozen source commit used for those builds. A separate TrueNAS SCALE
25.10.6 gate covered ZFS dataset permissions, fresh install, migration, and
rollback.

## External validation and release gates

The following still require empirical execution or an explicit controlled
release/production step:

- public GitHub and GHCR publication;
- `--execute`, production snapshots and production migration;
- empirical Custom App digest/promotion/rollback behavior.

The completed pre-publication TrueNAS validation followed this order: isolated
fresh side-by-side 0.5.2 install, private `0.5.0/0.5.1 -> 0.5.2` upgrade UAT,
then rollback validation. The same order remains the reference for future
candidates. A test database must have Web Push delivery isolated so no real
subscriber receives a UAT notification. See
[`../deploy/truenas/INSTALL.md`](../deploy/truenas/INSTALL.md) for the detailed
plan.
