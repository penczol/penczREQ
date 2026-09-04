# penczREQ 0.5.0 unpublished pre-release notes

Version 0.5.0 was a private validation candidate and was never published. These
notes are retained only as historical pre-release context; 0.5.2 is the planned
first public release.

## User experience

- Complete Polish and English Public UI with a saved per-user language choice.
- Polish and English localized TMDB titles are stored alongside the original
  title, with deterministic locale-to-original fallback and duplicate removal
  shared by desktop and mobile cards.
- Responsive request cards for ordinary users and administrators.
- Always-visible server-side pagination (25/50/100) for Requests, Upcoming and
  Completed, including empty lists and a compact single-row mobile variant.
- Precise withdrawal semantics: the request is deleted when its author is the
  only active interested user; otherwise only that user's participation and
  future notifications are removed while the request remains for others.
- Grouped TV seasons, filters, sorting and safe last-page correction.

## Administration and Control

- Role-based administration independent of usernames.
- Administrator restore action for accidentally completed requests.
- Separate private Control service for account lifecycle, recovery, IP blocks,
  encrypted settings, trusted proxies, backups and the security audit trail.
- Shared encrypted TMDB API Read Access Token configuration with safe connection
  diagnostics and live use by Public.
- Transactional, verified backup pairs for Public and Control databases.

## PWA and notifications

- Installable network-only PWA/WebAPK with a controlled service-worker update.
- Optional VAPID Web Push subscriptions and translated notifications.
- Authenticated pages, API responses, posters and account data are not cached
  for offline use.
- Physical Android Chrome UAT passed PWA/WebAPK installation, launch, Web Push
  and system notifications. A dedicated transparent monochrome notification
  badge now avoids a solid status-bar square while retaining the full-colour
  notification icon. Google Chrome is the recommended browser for installing
  penczREQ as a PWA on Android.
- Physical Samsung Internet UAT also passed installation, launch and Web Push.
  Its browser-generated Android package may show an older-app warning during
  installation, and TMDB/IMDb icons may render slightly lighter. Continuing
  completed installation normally. Samsung Internet remains supported but is
  not the preferred installation method. The packaging message is outside the
  penczREQ web manifest and service worker.
- iOS/iPadOS 16.4+ Home Screen web apps are expected to support Web Push in a
  secure context, but penczREQ has not yet been empirically verified there.

## Implemented deployment and release engineering

- Hardened non-root two-service Dockerfile/Compose contract. A real Linux/amd64
  image build, independent no-cache rebuild, Public/Control runtime inspection,
  SPDX image SBOM and Trivy scans completed local Windows 11 container
  validation with accepted, unfixed Debian base-image findings.
- Dry-run-first configurator whose empirical appliance target is TrueNAS SCALE
  25.10.4. Fresh install is the primary route for new users; 0.4.3 upgrade is
  retained for an existing legacy installation.
- Independent LAN/reverse-proxy choices for Public and Control.
- Deterministic installer/release artifacts, checksums and machine-readable
  release manifest.
- Prepared GitHub Actions contracts for tests, dependency/secret/vulnerability
  scans, hardened-image inspection, SPDX SBOM and gated GHCR promotion.
- Immutable numbered image as rollback anchor; `stable` only after an approved
  published release.

## Upgrade safety

The implemented upgrade path is designed to preserve existing secrets, validate
both SQLite databases, create verified paired backups and record a recursive ZFS
snapshot before mutation. Compose/schema/security-contract changes require the
migrator; a simple image-only update is reserved for compatible changes. This
path has not yet been executed on a real TrueNAS appliance.

This release specifically adds nullable `requests.title_en`; consequently the
legacy `0.4.3 -> 0.5.0` upgrade requires the versioned migrator and is not an
image-only update. Application startup applies the local idempotent schema
change, and the installer verifies the required column and both databases after
startup. The optional network title backfill remains a separately approved
operator action; `NULL` safely falls back to the original title in EN.

## Known external checks

Local final-image builds, image SBOM, image vulnerability scan and hardened
runtime inspection have been executed. License selection, public
repository/GHCR publication, a real TrueNAS SCALE 25.10.4 `app.create`, legacy
0.4.3 clone migration, clone rollback, `--execute`, production snapshots and
production upgrade remain pending controlled steps.

## Skrót po polsku

Wersja 0.5.0 dodaje pełny interfejs PL/EN, responsywne karty i paginację, dwa
warianty wycofania własnego requestu, prywatny panel Control, szyfrowaną wspólną
konfigurację TMDB, kopie obu baz oraz instalowalne PWA z Web Push. Przygotowano
utwardzony kontrakt Dockerfile/Compose, instalator TrueNAS działający domyślnie
jako dry-run i workflow CI/release dla sum, SBOM-u oraz skanów bezpieczeństwa.

Lokalne buildy finalnego obrazu, SBOM, skany i utwardzony runtime smoke zostały
empirycznie wykonane na Windows 11 z Docker Desktop. Publikacja GitHub/GHCR,
wybór licencji i migracja na prawdziwym TrueNAS SCALE 25.10.4 pozostają
kontrolowanymi krokami wydaniowymi/operacyjnymi.

This product uses the TMDB API but is not endorsed or certified by TMDB.
