# penczREQ 0.5.2 release notes

Version 0.5.2 is the candidate for the first public penczREQ release. Versions
0.5.0 and 0.5.1 were unpublished private candidates and are not public release
history. TrueNAS SCALE 25.10.6 fresh-install, private-upgrade, rollback, and
runtime gateway validation passed. The repository is licensed as
AGPL-3.0-only; publication remains controlled by the existing release approval
workflow.

## Runtime reverse-proxy trust

- Public and Control now resolve the current container-network gateway before
  Uvicorn configures forwarded-header handling. The same immutable startup
  snapshot is used by Uvicorn and the application authorization boundary.
- TrueNAS reverse-proxy mode opts the relevant service into this resolver. LAN
  mode and generic Compose deployments remain opted out by default.
- Automatic trust is limited to one directly connected, on-link RFC1918 IPv4
  default gateway and adds only its `/32`. Missing, ambiguous, malformed,
  off-link, loopback, global, reserved or network/broadcast candidates fail
  closed and produce a warning without expanding trust.
- The gateway is derived afresh at every container start. A Docker network
  recreation after update, redeploy or reboot therefore replaces the previous
  automatic peer without a manual environment-file correction.
- The runtime gateway is not persisted as a user setting. Manual trusted proxy
  entries remain separate, are merged and deduplicated with the current gateway,
  and changes made in Control take effect after the affected service restarts.
  Wildcards and `/0` remain forbidden.

## TrueNAS environment-file ownership

The cumulative 0.5.2 installer includes the private 0.5.1 correction: fresh
install and upgrade deterministically enforce `root:root` ownership and mode
`0600` for `public.env` and `control.env`, regardless of inherited parent-dataset
ownership. Existing secrets and already-current file bytes are preserved during
metadata repair. The runtime-owned VAPID private key retains its separate
application-user, mode-`0600` contract.

## Upgrade safety

The legacy `0.4.3 -> 0.5.2` path requires the versioned migrator and is not an
image-only update because it includes nullable `requests.title_en` and changes
the trusted-proxy/entry-point contract. The private `0.5.0` or `0.5.1` candidate
may also be used as the source
of an isolated 0.5.2 upgrade UAT. The installer validates and backs up both
databases, records a recursive ZFS rollback snapshot, preserves secrets and
manual/legacy trusted-proxy values, handles an exact `STOPPED` Custom App, and
requires both services and database checks to pass before reporting success.

## Cumulative application and release features

- Complete Polish and English UI with a saved per-user language choice,
  localized TMDB titles and translated notifications.
- Responsive, paginated request lists, grouped TV seasons, precise withdrawal
  semantics and an administrator restore action.
- Separate private Control service for accounts, recovery, encrypted settings,
  backups and the security audit trail.
- Installable network-only PWA/WebAPK with optional Web Push and no offline copy
  of authenticated data.
- Hardened non-root Public and Control containers, deterministic installer
  artifacts, checksums, SBOM and vulnerability gates, publication privacy checks,
  a sealed final-image build and approval-gated immutable-image promotion.

## Known external checks

Fresh install, isolated private upgrade, rollback, and runtime gateway checks
passed on TrueNAS SCALE 25.10.6. Public GitHub/GHCR publication and the container
vulnerability decision remain separate controlled gates; local publication
preparation does not publish them.

## Skrót po polsku

Wersja 0.5.2 jest kandydatem na pierwsze publiczne wydanie penczREQ; 0.5.0 i
0.5.1 pozostały prywatne i nieopublikowane. Przy każdym starcie kontenera usługa
w trybie reverse proxy wyznacza bieżący, bezpośrednio połączony prywatny gateway
Docker i ufa wyłącznie jego `/32`. Dzięki temu odtworzenie sieci po aktualizacji,
redeployu lub restarcie nie wymaga ręcznej korekty env, a poprzedni automatyczny
adres nie pozostaje zaufany. Wartość dynamiczna nie jest zapisywana jako
ustawienie użytkownika; ręczne wpisy są zachowane i zaczynają obowiązywać po
restarcie właściwej usługi. Brak jednoznacznego bezpiecznego gateway kończy się
fail-closed.

Kandydat zawiera także poprawkę 0.5.1: `public.env` i `control.env` mają
deterministycznie `root:root` oraz `0600`, bez rotacji sekretów i bez
niepotrzebnej zmiany bajtów plików. Fresh install, prywatny upgrade, rollback i
testy runtime gateway na TrueNAS SCALE 25.10.6 zakończyły się powodzeniem, a
projekt jest licencjonowany jako AGPL-3.0-only.

This product uses the TMDB API but is not endorsed or certified by TMDB.
