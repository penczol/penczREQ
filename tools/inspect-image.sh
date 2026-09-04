#!/bin/sh
set -eu

image="${1:-penczreq:0.5.2}"

docker image inspect "$image" --format \
  'id={{.Id}} size={{.Size}} user={{.Config.User}} version={{index .Config.Labels "org.opencontainers.image.version"}} source={{index .Config.Labels "org.opencontainers.image.source"}}'

if docker image inspect "$image" --format '{{range .Config.Env}}{{println .}}{{end}}' |
  grep -E '(SECRET|PASSWORD|TOKEN|TMDB)'; then
  echo "A secret-like variable is embedded in the image." >&2
  exit 1
fi

docker run --rm --entrypoint sh "$image" -c '
  test "$(id -u)" = 568
  ! command -v pip
  ! test -e /app/tests
  ! test -e /app/tools
  ! test -e /app/deploy
  python -c "from request_app import __version__; assert __version__ == \"0.5.2\""
  find /app -maxdepth 1 -mindepth 1 -printf "%f\n" | sort
'

if docker history --no-trunc --format '{{.CreatedBy}}' "$image" |
  grep -Ei '(SESSION_SECRET|CONTROL_SESSION_SECRET|CONFIG_ENCRYPTION_KEY|BOOTSTRAP_PASSWORD|TMDB_TOKEN)'; then
  echo "A secret-like value is present in image history." >&2
  exit 1
fi
