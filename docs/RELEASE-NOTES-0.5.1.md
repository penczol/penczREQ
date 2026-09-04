# penczREQ 0.5.1 release notes

Version 0.5.1 was an unpublished private intermediate candidate. It is retained
as pre-release history because its deterministic environment-file ownership fix
is included cumulatively in 0.5.2. Version 0.5.1 was never published and must not
be treated as public release history.

## User experience

- Complete Polish and English Public UI with a saved per-user language choice.
- Localized TMDB titles, responsive request cards, grouped TV seasons, filters,
  sorting and server-side pagination for all three main lists.
- Precise withdrawal semantics and an administrator restore action for
  accidentally completed requests.
- Separate private Control service for accounts, recovery, encrypted settings,
  trusted proxies, transactional backups and the security audit trail.

## PWA and notifications

- Installable network-only PWA/WebAPK with optional translated Web Push.
- Authenticated views, API responses, posters and account data are never cached
  for offline use.
- The README now explains that content blockers, DNS/HTTPS filters and local-VPN
  filtering can interfere with browser installability checks, and recommends
  allowlisting the penczREQ domain before retrying. Version 0.5.1 does not change
  the manifest, service worker, icons or other PWA application source.

## TrueNAS environment-file ownership

The TrueNAS installer now enforces `root:root` ownership and mode `0600` for
`public.env` and `control.env` during both fresh install and upgrade, regardless
of inherited parent-dataset ownership. The policy is explicit at the two secret
environment-file call sites and does not change generic atomic writes.

During upgrade, already-current env content is not replaced merely to repair
metadata. Existing session and encryption secrets remain unchanged. The VAPID
private key keeps its separate runtime contract: application-user ownership and
mode `0600`.

## Upgrade safety

Version 0.5.1 makes no database-schema change relative to the unpublished 0.5.0
candidate. The legacy `0.4.3 -> 0.5.1` path still includes nullable
`requests.title_en`, so it requires the versioned migrator and is not an
image-only update. The migrator validates both databases, creates a consistent
backup pair and ZFS rollback snapshot, preserves secrets and handles an exact
`STOPPED` Custom App before requiring `RUNNING`.

The internal `0.5.0 -> 0.5.1` upgrade is a private validation route, not public
release history. It specifically verifies metadata repair, preservation and the
future update mechanism.

## Implemented release engineering

- Hardened non-root Public and Control container contract.
- Deterministic installer and release artifacts with SHA-256 checksums.
- Prepared CI and release workflows for tests, dependency and secret scans,
  publication privacy checks, final-image build, SPDX SBOM, vulnerability scans
  and approval-gated immutable-image promotion.
- Numbered image tags remain rollback anchors; a push to `main` never updates a
  TrueNAS installation.

## Historical validation status

The 0.5.1 local source, installer artifact and OCI candidate completed their
local gates, but no GitHub/GHCR publication or production update was performed.
The planned first-public candidate moved to 0.5.2 before appliance UAT. Current
manual validation requirements are documented in the 0.5.2 release notes and
TrueNAS installer guide.

## Skrót po polsku

Wersja 0.5.1 była nieopublikowanym, prywatnym kandydatem pośrednim. Instalator
TrueNAS jawnie wymuszał `root:root` i `0600` dla `public.env` oraz `control.env`
podczas fresh install i upgrade, bez rotacji sekretów i bez niepotrzebnej zmiany
treści plików. Publiczny README zawierał neutralną diagnostykę filtrów treści,
DNS/HTTPS i local VPN dla instalacji PWA; kod PWA nie został zmieniony. Poprawki
te są częścią skumulowanego kandydata 0.5.2 na pierwsze publiczne wydanie.

This product uses the TMDB API but is not endorsed or certified by TMDB.
