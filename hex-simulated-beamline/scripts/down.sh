#!/usr/bin/env bash
# Tear down the simulated HEX services stack. Add --volumes to also drop data.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"
extra=()
[[ "${1:-}" == "--volumes" ]] && extra+=(--volumes)
echo "[hexsim] stopping services..."
docker compose -f "$root/compose/docker-compose.yml" down "${extra[@]}"
# Throw away the ephemeral self-signed sim cert (regenerated fresh on next up.sh).
rm -f "$root/compose/certs/redis.crt" "$root/compose/certs/redis.key"
echo "[hexsim] stopped. (Stop the blackhole IOC separately with Ctrl-C if running.)"
