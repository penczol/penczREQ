# Update, migration and rollback model

This document defines the release-to-update contract for penczREQ 0.5.2.
Production deployment is never automatic and requires explicit operator
approval for the concrete operation.

Fresh install is the primary route for new users because 0.5.2 was the
first public release. Upgrade `0.4.3 -> 0.5.2` is retained as the legacy
migration path and as the reference versioned-migrator contract; 0.4.3, 0.5.0
and 0.5.1 were not public releases.

## TrueNAS target and API contract

The installer consumes the TrueNAS 25.10 API namespace and its technical
preflight currently accepts the TrueNAS SCALE 25.10.x branch. The empirically
validated installer target is **TrueNAS SCALE 25.10.6**. Fresh install, private
upgrade, rollback and the ZFS dataset contract passed for the 0.5.2 candidate.
The Custom App image-update and data-recovery tests also passed on **25.10.7**.

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

The numbered tag is the rollback anchor. Record its registry digest as well. The `stable` channel
starts at 0.5.2. All later images on that channel must work with the YAML,
data and configuration of every earlier release on the same channel,
including installations that skipped releases.
Publishing is triggered only by a published GitHub Release, runs in the protected
`release` environment and requires a selected repository `LICENSE`.

The workflow separates verification from publication. The verification job has
read-only repository access, disables persisted checkout credentials and rejects
a release commit that is not contained in `origin/main`. It seals the tested image
and evidence into one short-lived workflow artifact. A dependent job in the
protected `release` environment is the only job with `contents: write` and
`packages: write`; it verifies that artifact before pushing the immutable image,
and uploading the fixed release-asset list. The last step promotes that
numbered manifest by digest to the channel and reads its digest back.

### Publishing a release

1. Set the application and installer version, refresh release notes and review
   `deploy/truenas/update-policy.json` for that version. Keep the channel
   baseline only after testing an image-only update from the oldest supported
   release and the preceding release. Check both services and rollback.
2. For any incompatible change listed below, set the baseline to the new
   version. List schema changes in the policy. Packaging assigns a separate
   `stable-X.Y.Z` tag to the new baseline and its compatible successors.
   The old channel stays on its last compatible release until its operator
   backs up, runs the versioned migrator, verifies the result and enrolls in
   the new channel. A version change without a policy review fails packaging.
3. After reviewed changes reach main and CI passes, tag that commit `vX.Y.Z`
   and publish a final GitHub Release for that tag. Review the verify job and
   approve its protected **release** deployment. GitHub Actions rejects a
   prerelease, a tag/version mismatch and a commit outside main history.
4. Check the publish job, release assets and `image-digest.txt`. The numbered
   and channel manifests must resolve to that digest. A different image ID
   under an existing numbered tag stops publication. Resume a failed publish
   from the sealed candidate while it is available.

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
For routine updates use the reviewed channel from the release manifest in
both services. Leave the registered YAML, env files and bind mounts in place.

### Updating in TrueNAS Apps

In Apps → Configuration → Settings, verify that image-update checks are
enabled. When the installed Custom App shows **Update available**, select
that app and **Update**. Avoid **Update All** when testing a single app.
The registry polling interval can delay the badge. A numbered tag, a local
RC name, a digest pin or `pull_policy: never` does not enroll an app in the
moving channel. A one-time enrollment of an existing installation requires
the version, storage and rollback checks below.

After the job, verify both container healthchecks, the application version,
image IDs and registry digest, and a known persisted record. The TrueNAS
Custom App catalog version is separate from the version inside the image.
If an update fails or is interrupted, inspect the app job, health and running
digest of **both** services before retrying. Stop mixed versions or unhealthy
services before resuming writes. Restore the saved image digest and YAML.
Restore matching data and env files if the persistent state changed. The Apps
rollback option does not restore mounted host-path data. See the
[Apps UI](https://www.truenas.com/docs/scale/25.10/scaleuireference/apps/) and
[image-pull API](https://api.truenas.com/v25.10/api_methods_app.pull_images.html).
In 25.10.7 the Custom App branch of `app.upgrade` calls Compose pull and
redeploy before the catalog snapshot path. Its image-update flag can clear
after the pull even when redeployment fails. In a 25.10.7 failure test the outer
`app.upgrade` job reported SUCCESS, the child `app.redeploy` job reported FAILED,
Control exited and Public was unavailable. Check both jobs and both healthchecks.
See the
[upgrade implementation](https://github.com/truenas/middleware/blob/TS-25.10.7/src/middlewared/middlewared/plugins/apps/upgrade.py)
and [pull implementation](https://github.com/truenas/middleware/blob/TS-25.10.7/src/middlewared/middlewared/plugins/apps/pull_images.py).

An isolated two-service Custom App on 25.10.7 followed a moving GHCR test tag.
After a targeted native registry check, Apps offered **Update**. Approving it
recreated both services on the published digest. The saved request, Public and
Control accounts, language settings, registered YAML and both env files survived.
An independent legacy-schema fixture verified the startup migration and reversal
from a stopped-state snapshot. After the failed-image test, restoring the matching
dataset snapshot and pinning the previous manifest restored service. The operator
re-enrolled the original YAML after the moving tag pointed to the verified image.
The test did not measure the background registry polling interval.

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
   for a migration, quiesce both services before backing up the live pair
   and taking the snapshot so writes cannot fall between recovery points;
2. run SQLite `quick_check` and `foreign_key_check` on both databases;
3. create a transactionally consistent backup pair and verify it;
4. create a recursive ZFS snapshot for the application root dataset;
5. retain the previous registered YAML and image digest alongside the
   installer's `ROLLBACK.json`, which records backup paths and snapshot;
6. verify disk space and retain the previous numbered image;
7. obtain explicit approval for the exact production action.

## Rollback

Stop after any failed health or integrity check. Rollback restores one coherent
application state:

1. stop the Custom App;
2. restore the recorded snapshots for the application root and its child datasets
   when the change affected schema, configuration, keys or other persistent files.
   A matching SQLite pair alone suffices only if databases were the only state
   that changed and the matching configuration and keys are available;
3. restore the previous registered Compose and pin the previous manifest digest.
   For an imported RC, retain its Docker archive, checksum, local tag and image ID;
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

- approval and verification of each new public GitHub/GHCR release;
- production enrollment, snapshots and migration for the specific installation;
- compatibility and recovery for each new release candidate.

The completed pre-publication TrueNAS validation followed this order: isolated
fresh side-by-side 0.5.2 install, private `0.5.0/0.5.1 -> 0.5.2` upgrade UAT,
then rollback validation. The same order remains the reference for future
candidates. A test database must have Web Push delivery isolated so no real
subscriber receives a UAT notification. See
[`../deploy/truenas/INSTALL.md`](../deploy/truenas/INSTALL.md) for the detailed
plan.
