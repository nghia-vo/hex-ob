#!/usr/bin/env bash
# Bring up the simulated HEX services stack (Redis/Mongo/Kafka/Tiled), wait for
# health, and seed Redis + the Tiled catalog. Idempotent — safe to re-run.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"

# Self-signed cert for the TLS Redis on 6380 (generated once; git-ignored).
certdir="$root/compose/certs"
if [ ! -f "$certdir/redis.crt" ]; then
    echo "[hexsim] generating self-signed Redis TLS cert..."
    mkdir -p "$certdir"
    # Subject clearly marks this as a throwaway SIMULATED cert, not a real key.
    openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
        -keyout "$certdir/redis.key" -out "$certdir/redis.crt" \
        -subj "/O=NSLS-II HEX beamline SIMULATION (hxm_program)/OU=SIMULATED - NOT A REAL KEY - self-signed throwaway/CN=hexsim-redis.simulated" \
        -addext "subjectAltName=DNS:xf27id1-hex-redis1.nsls2.bnl.gov,IP:127.0.0.1" \
        >/dev/null 2>&1
    chmod 644 "$certdir/redis.key" "$certdir/redis.crt"
fi

echo "[hexsim] starting services (docker compose)..."
docker compose -f "$root/compose/docker-compose.yml" up -d --wait

# Tiled has no in-compose healthcheck; poll its /healthz from the host.
echo -n "[hexsim] waiting for Tiled"
for _ in $(seq 1 60); do
    if curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
        echo " OK"; break
    fi
    echo -n "."; sleep 1
done

echo "[hexsim] seeding Redis + Tiled..."
bash "$here/seed.sh"

cat <<EOF

[hexsim] Services are up:
  Redis    127.0.0.1:6380  (TLS, secure — matches the new NSLS-II machines)
  MongoDB  127.0.0.1:27017
  Kafka    127.0.0.1:9092
  Tiled    http://127.0.0.1:8000  (api key: secret)

Next:
  1) source $root/scripts/env.sh
  2) $root/iocs/blackhole/run_blackhole.sh   # start the PV blackhole IOC
Stop everything with: $root/scripts/down.sh
EOF
