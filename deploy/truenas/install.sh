#!/usr/bin/env bash

set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "${SCRIPT_DIR}/installer.py" || ! -f "${SCRIPT_DIR}/compose.yaml.example" ]]; then
    printf 'BŁĄD: paczka instalatora penczREQ jest niekompletna.\n' >&2
    exit 1
fi

exec python3 "${SCRIPT_DIR}/installer.py" "$@"
