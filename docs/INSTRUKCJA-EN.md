# penczREQ 0.5.2 — user and administrator guide

[Polski](INSTRUKCJA-PL.md) | [English](INSTRUKCJA-EN.md)

## Purpose and architecture

penczREQ is a self-hosted, manually operated media-request manager for
Jellyfin. Users search for films and TV series in TMDB, register their interest
and follow fulfilment. The application does not download files, inspect a
Jellyfin library or integrate with Sonarr/Radarr.

The system consists of two services:

- **Public** (`8000`) — the user interface and ordinary request administration;
- **Control** (`8001`) — the private security and configuration panel.

Control has a separate account, sessions and database. It must not be exposed
to the Internet.

## Windows DEV/UAT

Python 3.12 and Node.js are required. After installing the dependencies, start
the services in two PowerShell windows:

```powershell
.\start-dev.ps1
```

```powershell
.\start-control-dev.ps1
```

Addresses:

- Public: <http://127.0.0.1:8000>
- Control: <http://127.0.0.1:8001>

On its first start, Control writes one-time credentials to
`dev-data/control/CONTROL-FIRST-LOGIN.txt`. The file is deleted automatically
after the mandatory password change. Do not copy its contents into the
repository, an image or a message.

## Accounts, roles and sessions

Authorization depends only on the role stored in the database — a username does
not grant administrator privileges. Control can create, disable and restore
accounts, change roles and transfer the administrator role. Security operations
invalidate the relevant sessions.

A password contains 15–128 ASCII characters, including a lowercase letter, an
uppercase letter and a digit. Public and Control use separate cookies and
session secrets. Sensitive Control operations require the current password to
be entered again.

The sign-in limiter does not reveal whether an account exists. Ten failed
attempts from one address within 10 minutes block the address for 15 minutes;
further cycles within 24 hours extend the block up to 24 hours. IPv6 is grouped
by `/64` prefix. Blocks can be reviewed and removed in Control.

## English and Polish

Each user independently selects PL or EN. The selection covers Public,
notifications, error messages and system-generated content. Control also has a
complete PL/EN interface. Historical notifications of recognized types are
normalized to current templates without altering an administrator's custom
messages.

Request cards and TMDB search use the title localized for the selected language
as the main title. The original title appears underneath only when it is
meaningfully different. Existing records that have not yet received a stored
English localization safely fall back to the original title; list rendering
does not make background TMDB requests.

## Requests and participation

Public has three main tabs: `Requests`, `Upcoming` and `Completed`. Each uses
server-side sorting and a 25/50/100 paginator, including an empty list. The
status filter applies to Requests. A TV series can display grouped seasons.

A user can create a request or join an existing one. `Withdraw my request` has
two possible results:

- if the user is the only active interested person, the request is deleted;
- if other users remain interested, the request stays, while the withdrawing
  user removes only their participation and receives no further notifications
  about that request.

The administrator's `Restore to requests` is a separate operation that reverses
an accidental Completed state. It is not the inverse of a user's withdrawal.

## TMDB API Read Access Token

penczREQ requires a **TMDB API Read Access Token** used as a Bearer token — not
the old v3 API Key. The token is saved and tested in Control. It is encrypted
with AES-GCM in the settings database, is not returned by the API or settings
history, and does not need to be duplicated for Public. Public reads the current
shared configuration when searching for films, TV series, details and seasons.

## LAN and reverse-proxy modes

Public and Control independently support `lan` and `reverse-proxy`, so all four
combinations are valid:

| Public | Control | Use |
| --- | --- | --- |
| LAN | LAN | direct private HTTP access |
| reverse proxy | LAN | Public over HTTPS, Control directly in the LAN |
| LAN | reverse proxy | Public over HTTP, private Control through an HTTPS proxy |
| reverse proxy | reverse proxy | both proxied, Control still limited to LAN/VPN |

LAN requires an HTTP URL and `COOKIE_SECURE=false`. Reverse proxy requires
HTTPS and `COOKIE_SECURE=true`. On TrueNAS the relevant service resolves the
current directly connected private container gateway at every startup and
trusts only its `/32`; explicit manual peers remain additive. A network
recreation therefore needs a service restart, not a manual gateway correction.
Manual changes made in Control take effect after the affected service restarts.
Ambiguous route data, wildcards and `/0` fail closed. Control in reverse-proxy
mode is not Internet exposure — the final client must still belong to the
private LAN/VPN allowlist.

## PWA and Web Push

Public can be installed as a PWA/WebAPK from the account section or a supported
browser's menu. Android Chrome was empirically verified on a physical device:
opening the site, PWA/WebAPK installation, launching the installed application,
Web Push and system notifications all passed.

Google Chrome is the recommended browser for installing penczREQ as a PWA on Android.

Samsung Internet was also empirically verified: installation, launch and Web
Push worked, so it remains supported but is not the preferred installation
method. The browser-generated package may display an Android warning about an
app intended for an older system version during installation, and the TMDB/IMDb
icons may render slightly lighter. That wrapper/platform message is outside the
penczREQ manifest and service worker; in UAT, installation completed normally
after choosing to continue. On iOS/iPadOS 16.4+, Web Push is available to an
installed Home Screen web app where the platform and browser requirements are
met; penczREQ has still not been empirically verified on iOS/iPadOS.

Installation and Web Push require a secure HTTPS context, except for the
`localhost` exception. Plain HTTP LAN remains usable as a web application but
does not guarantee PWA/Push on another device. The PWA remains a network client:
it does not cache private views, API responses, posters or account data for
offline use. A service-worker update requires a controlled refresh accepted by
the user. penczREQ does not use TWA, a separate APK or a wrapper.

Platform support sources: [WebKit — Web Push for iOS/iPadOS 16.4+ Home Screen
web apps](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/)
and [Samsung Internet — PWA and Web Push](https://developer.samsung.com/internet/android/web-developer-guide.html).

## Data, backups and recovery

In DEV, the main database is `dev-data/app.db` and the Control database is
`dev-data/control/control.db`. Posters are in `dev-data/posters/`, JSONL logs in
`dev-data/logs/`, and backups in `dev-data/backups/`.

Control creates a transactional pair of both databases and checks each one with
SQLite `quick_check`. Recovery after operations affecting accounts, settings or
security must restore both databases from the same set — never only one.
Retention for backups and logs is configured in Control. Secrets, passwords and
keys are redacted in the audit trail.

## TrueNAS, update and rollback

Fresh install is the primary route for new 0.5.2 users. The TrueNAS configurator
is dry-run by default; mutations require explicit `--execute` on the NAS and
operator approval for that concrete change. The validator technically accepts
the TrueNAS SCALE 25.10.x branch, while TrueNAS SCALE 25.10.6 is the empirically
validated target.

Upgrade `0.4.3 -> 0.5.2` remains the migration path for an existing legacy
installation. It is not required for a new user starting with 0.5.2. The
detailed guide is in
[`../deploy/truenas/INSTALL.md`](../deploy/truenas/INSTALL.md).

A simple image-only update is eligible only when Compose, ports, mounts, secrets
and database contracts remain compatible. A change to any of those surfaces
requires the versioned migrator, both database checks, a consistent backup pair,
a ZFS snapshot and a recorded rollback plan. The numbered image is the rollback
anchor; `:stable` points to the most recently approved release. See
[`UPDATE.md`](UPDATE.md).

## Tests and diagnostics

Run the complete local suite with:

```powershell
.\security-test.ps1
```

It covers application regression and the runtime smoke for both services, CSRF,
reauthentication, secrets, backups and integrity. The prepared CI workflow also
checks Python/JavaScript/shell syntax, dependencies, secrets, private
identifiers and configuration. On a runner with a container engine it is also
configured to build the image, generate an SPDX SBOM, inspect and scan the
image. Those operations were executed empirically on Windows 11 with Docker
Desktop for the frozen container baseline, including hardened Public/Control smoke
and an independent rebuild. Separate TrueNAS SCALE 25.10.6 validation covered
fresh install, upgrade, rollback, and ZFS dataset behavior. A red scan result
must not be presented as a successful release.

## Fixed security boundaries

- Control is not a public Internet management panel.
- Containers do not receive a Docker socket, TrueNAS API, Jellyfin data or
  Caddy configuration.
- Tokens, passwords, keys and bootstrap credentials are not stored in Compose
  or Git.
- The installer does not change Caddy, Jellyfin or the router.
- GitHub/GHCR publication is not automatic, the project is licensed under
  AGPL-3.0-only, and a production change requires explicit operator approval.

This product uses the TMDB API but is not endorsed or certified by TMDB.
