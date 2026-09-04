#!/bin/sh
set -eu

image="${1:-penczreq:0.5.2}"
prefix="penczreq-v050-smoke"
network="${prefix}-net"
public_name="${prefix}-public"
control_name="${prefix}-control"
test_root="/tmp/${prefix}"
host_uid="$(id -u)"
host_gid="$(id -g)"

case "$test_root" in
  /tmp/penczreq-v050-smoke) ;;
  *) echo "Unsafe test path." >&2; exit 2 ;;
esac

cleanup_containers() {
  docker rm -f "$public_name" "$control_name" >/dev/null 2>&1 || true
}

set_test_root_ownership() {
  owner="$1"
  [ -d "$test_root" ] || return 0
  [ ! -L "$test_root" ] || return 1
  docker run --rm \
    --network none \
    --read-only \
    --user 0:0 \
    --cap-drop ALL \
    --cap-add CHOWN \
    --security-opt no-new-privileges:true \
    --mount "type=bind,src=$test_root,dst=/smoke-data" \
    --entrypoint chown \
    "$image" \
    -R "$owner" /smoke-data
}

cleanup_all() {
  cleanup_containers
  docker network rm "$network" >/dev/null 2>&1 || true
  set_test_root_ownership "${host_uid}:${host_gid}" >/dev/null 2>&1 || true
  rm -rf -- "$test_root" || true
}

cleanup_on_exit() {
  status="$1"
  trap - EXIT INT TERM
  cleanup_all
  exit "$status"
}

trap 'cleanup_on_exit $?' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cleanup_all
mkdir -p "$test_root/app" "$test_root/control" "$test_root/backups"
set_test_root_ownership 568:568
docker network create --subnet 172.29.43.0/24 "$network" >/dev/null

session_secret="$(head -c 48 /dev/urandom | base64 | tr -d '\n')"
control_session_secret="$(head -c 48 /dev/urandom | base64 | tr -d '\n')"
config_key="$(head -c 48 /dev/urandom | base64 | tr -d '\n')"
public_password="PublicStartPassword2026Z" # pragma: allowlist secret
control_password="ControlStartPassword2026Z" # pragma: allowlist secret

start_control() {
  public_bootstrap_password="$1"
  control_bootstrap_password="$2"
  docker run -d \
    --name "$control_name" \
    --network "$network" \
    --read-only \
    --init \
    --user 568:568 \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777 \
    --pids-limit 256 \
    --mount "type=bind,src=$test_root/app,dst=/data" \
    --mount "type=bind,src=$test_root/control,dst=/control-data" \
    --mount "type=bind,src=$test_root/backups,dst=/backups" \
    -e APP_ENV=production \
    -e APP_COMPONENT=control \
    -e APP_BASE_URL=https://penczreq.test \
    -e CONTROL_BASE_URL=http://control.test:8001 \
    -e PUBLIC_ACCESS_MODE=reverse-proxy \
    -e CONTROL_ACCESS_MODE=lan \
    -e COOKIE_SECURE=false \
    -e DATA_DIR=/data \
    -e CONTROL_DATA_DIR=/control-data \
    -e BACKUP_DIR=/backups \
    -e LOG_DIR=/data/logs \
    -e ALLOWED_HOSTS=penczreq.test,127.0.0.1,localhost \
    -e CONTROL_ALLOWED_HOSTS=control.test,127.0.0.1,localhost \
    -e CONTROL_ALLOWED_NETWORKS=127.0.0.0/8,172.29.43.0/24 \
    -e CONTROL_TRUSTED_PROXIES= \
    -e CONFIG_ENCRYPTION_KEY="$config_key" \
    -e CONTROL_SESSION_SECRET="$control_session_secret" \
    -e PUBLIC_ADMIN_USERNAME=smoke-admin \
    -e PUBLIC_ADMIN_BOOTSTRAP_PASSWORD="$public_bootstrap_password" \
    -e CONTROL_ADMIN_USERNAME=smoke-control \
    -e CONTROL_BOOTSTRAP_PASSWORD="$control_bootstrap_password" \
    -e TZ=Europe/Warsaw \
    "$image" \
    python -m request_app.server control \
      --host 0.0.0.0 --port 8001 >/dev/null
}

start_public() {
  docker run -d \
    --name "$public_name" \
    --network "$network" \
    --read-only \
    --init \
    --user 568:568 \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777 \
    --pids-limit 256 \
    --mount "type=bind,src=$test_root/app,dst=/data" \
    -e APP_ENV=production \
    -e APP_COMPONENT=public \
    -e APP_BASE_URL=https://penczreq.test \
    -e CONTROL_BASE_URL=http://control.test:8001 \
    -e PUBLIC_ACCESS_MODE=reverse-proxy \
    -e CONTROL_ACCESS_MODE=lan \
    -e COOKIE_SECURE=true \
    -e DATA_DIR=/data \
    -e LOG_DIR=/data/logs \
    -e ALLOWED_HOSTS=penczreq.test,127.0.0.1,localhost \
    -e SESSION_SECRET="$session_secret" \
    -e CONFIG_ENCRYPTION_KEY="$config_key" \
    -e VAPID_SUBJECT=mailto:smoke@example.invalid \
    -e TZ=Europe/Warsaw \
    "$image" \
    python -m request_app.server public \
      --host 0.0.0.0 --port 8000 >/dev/null
}

wait_internal_health() {
  name="$1"
  port="$2"
  attempts=0
  while [ "$attempts" -lt 30 ]; do
    if docker exec "$name" python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${port}/internal/health', timeout=2)" \
      >/dev/null 2>&1; then
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 1
  done
  docker logs "$name" >&2 || true
  return 1
}

request_status() {
  target="$1"
  host="$2"
  path="$3"
  expected="$4"
  actual="$(docker run --rm --network "$network" --entrypoint python "$image" -c \
    "import urllib.error,urllib.request
r=urllib.request.Request('http://${target}${path}', headers={'Host':'${host}'})
try:
 print(urllib.request.urlopen(r, timeout=3).status)
except urllib.error.HTTPError as e:
 print(e.code)")"
  [ "$actual" = "$expected" ] || {
    echo "Unexpected status for ${target}${path}: ${actual}, expected ${expected}" >&2
    exit 1
  }
}

start_control "$public_password" "$control_password"
wait_internal_health "$control_name" 8001
start_public
wait_internal_health "$public_name" 8000

request_status "${public_name}:8000" penczreq.test /login 200
request_status "${public_name}:8000" penczreq.test /health 404
request_status "${public_name}:8000" penczreq.test /internal/health 404
request_status "${public_name}:8000" penczreq.test /posters/missing.jpg 401
request_status "${control_name}:8001" control.test /login 200

docker exec "$public_name" sh -c \
  "test ! -e /control-data && test ! -e /backups && ! env | grep -Eq '^(CONTROL_SESSION_SECRET|CONTROL_BOOTSTRAP_PASSWORD)='"
docker exec "$control_name" sh -c "! env | grep -q '^SESSION_SECRET='"

if docker exec "$public_name" sh -c "touch /app/should-not-exist" >/dev/null 2>&1; then
  echo "Read-only root filesystem test failed." >&2
  exit 1
fi

docker exec "$public_name" sh -c \
  "grep '^Uid:' /proc/1/status | grep -q '568.*568.*568.*568' &&
   grep '^CapEff:' /proc/1/status | grep -q '0000000000000000' &&
   grep '^NoNewPrivs:' /proc/1/status | grep -q '1'"

admin_count="$(docker exec "$public_name" python -c \
  "import sqlite3; c=sqlite3.connect('/data/app.db'); print(c.execute(\"select count(*) from users where role='admin'\").fetchone()[0])")"
[ "$admin_count" = "1" ]

docker exec "$public_name" python -c \
  "import sqlite3; c=sqlite3.connect('/data/app.db'); assert c.execute('pragma quick_check').fetchone()[0]=='ok'"
docker exec "$control_name" python -c \
  "import sqlite3; c=sqlite3.connect('/control-data/control.db'); assert c.execute('pragma quick_check').fetchone()[0]=='ok'"

attempts=0
while [ "$attempts" -lt 10 ] && ! find "$test_root/backups" -type f -name '*.db' | grep -q .; do
  attempts=$((attempts + 1))
  sleep 1
done
[ "$(find "$test_root/backups" -type f -name '*.db' | wc -l)" -ge 2 ]

# Recreate both containers with the same datasets and with bootstrap passwords removed.
cleanup_containers
start_control "" ""
wait_internal_health "$control_name" 8001
start_public
wait_internal_health "$public_name" 8000

admin_count_after="$(docker exec "$public_name" python -c \
  "import sqlite3; c=sqlite3.connect('/data/app.db'); print(c.execute(\"select count(*) from users where role='admin'\").fetchone()[0])")"
[ "$admin_count_after" = "$admin_count" ]
docker exec "$control_name" python -c \
  "import sqlite3; c=sqlite3.connect('/control-data/control.db'); assert c.execute('select count(*) from control_users').fetchone()[0]==1"

printf '%s\n' '{"status":"ok","checks":"clean-start+least-privilege+private-health+persistence+backup+integrity"}'
