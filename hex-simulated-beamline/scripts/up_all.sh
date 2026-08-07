#!/usr/bin/env bash
# ONE-SHOT bring-up of the ENTIRE simulated HEX beamline. Idempotent: running
# pieces are detected and left alone, missing ones are started. Canonical
# step-by-step (and what each piece is) lives in PROGRESS.md; this script IS
# that pre-flight, automated.
#
#   ./scripts/up_all.sh
#
# Env knobs:
#   HEX_PROFILE_MANIFEST  pixi.toml of hex-profile-collection (for sim_ioc's
#                         ophyd-async env). Default: ~/git_projects/hex-profile-collection/pixi.toml
#   HEX_SIM_TOOLENV       python env for the helper scripts (auto-created venv
#                         with caproto/pyepics/pandablocks/h5py if missing).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"
PROFILE_MANIFEST="${HEX_PROFILE_MANIFEST:-$HOME/git_projects/hex-profile-collection/pixi.toml}"
TOOLENV="${HEX_SIM_TOOLENV:-$root/.toolenv}"

say() { echo "[up_all] $*"; }

# ---- tool env (helper scripts need caproto/pyepics/pandablocks/h5py) --------
if [ ! -x "$TOOLENV/bin/python" ]; then
    say "creating tool env at $TOOLENV (one-time)..."
    python3 -m venv "$TOOLENV"
    "$TOOLENV/bin/pip" -q install caproto pyepics pandablocks h5py numpy
fi
PY="$TOOLENV/bin/python"

# ---- 0. data dir + services + experiment identity ---------------------------
mkdir -p /tmp/hex-sim-data && chmod 777 /tmp/hex-sim-data
say "services (Redis/Mongo/Kafka/Tiled) + sim sync-experiment..."
"$here/up.sh" >/dev/null
say "  services OK"

# ---- 1. PandA pair (engine sim :8888/:9101 + pandablocks-ioc CA :5095) ------
say "panda pair (may take ~30 s on first start)..."
COMPOSE_IGNORE_ORPHANS=1 docker compose -f "$root/compose/docker-compose.panda.yml" up -d 2>/dev/null
# Robust against transient docker-logs hiccups on a long-running container
# (seen 2026-08-07: marker present but the log grep failed once and aborted a
# healthy sim): an IOC container that has already been up >5 min counts as
# initialized even if the marker grep fails.
panda_up() {
    docker logs hexsim-panda-ioc 2>/dev/null | grep -q "All initialization complete" && return 0
    started=$(docker inspect --format '{{.State.StartedAt}}' hexsim-panda-ioc 2>/dev/null) || return 1
    [ -n "$started" ] && [ $(( $(date +%s) - $(date -d "$started" +%s) )) -gt 300 ]
}
for i in $(seq 60); do
    panda_up && break
    [ "$i" = 60 ] && { say "ERROR: panda-ioc never finished init (docker logs hexsim-panda-ioc)"; exit 1; }
    sleep 2
done
say "  panda pair OK"

# ---- 2. Kinetix frame tier (real AD IOC, CA :5085) ---------------------------
if ! docker image inspect hexsim-kinetix-ioc:local >/dev/null 2>&1; then
    say "ERROR: image hexsim-kinetix-ioc:local missing — build it once via the"
    say "       nsls2.ioc_deploy steps in PROGRESS.md (Kinetix frame tier)."
    exit 1
fi
say "kinetix AD IOC..."
COMPOSE_IGNORE_ORPHANS=1 docker compose -f "$root/compose/docker-compose.kinetix.yml" up -d 2>/dev/null
for i in $(seq 60); do
    docker logs hexsim-kinetix-ioc 2>&1 | grep -q "completed startup" && break
    [ "$i" = 60 ] && { say "ERROR: kinetix IOC never finished startup (docker logs hexsim-kinetix-ioc)"; exit 1; }
    sleep 2
done
say "  kinetix AD IOC OK"

# ---- 3. motor IOC (:5075) ----------------------------------------------------
if ! ss -uln | grep -q "127.0.0.1:5075 "; then
    say "starting motor IOC..."
    EPICS_CAS_SERVER_PORT=5075 nohup "$PY" "$root/iocs/motor/motor_ioc.py" \
        > /tmp/hex-motor.log 2>&1 &
    sleep 4
fi
say "  motor IOC OK"

# ---- 4. block design + IOC-level inits (safe to rerun) -----------------------
"$PY" "$root/iocs/panda/hex_tomo_design.py" >/dev/null
EPICS_CA_ADDR_LIST=127.0.0.1:5095 "$PY" "$root/iocs/panda/init_panda_ioc.py" >/dev/null
EPICS_CA_ADDR_LIST=127.0.0.1:5085 "$PY" "$root/iocs/kinetix/init_kinetix.py" >/dev/null
say "  design + inits OK"

# ---- 5. motor->INENC bridge ---------------------------------------------------
if ! pgrep -f "motor_encoder_bridge.py" >/dev/null; then
    say "starting bridge..."
    EPICS_CA_AUTO_ADDR_LIST=NO EPICS_CA_ADDR_LIST=127.0.0.1:5075 \
        nohup "$PY" -u "$root/iocs/panda/motor_encoder_bridge.py" \
        > /tmp/hex-bridge.log 2>&1 &
    sleep 3
fi
say "  bridge OK"

# ---- 6. sim_ioc (shutters/metadata blackhole + typed Det:3) -------------------
if ! pgrep -f "sim_ioc.py" >/dev/null; then
    if [ -f "$PROFILE_MANIFEST" ]; then
        say "starting sim_ioc (pixi env: $PROFILE_MANIFEST)..."
        BLACKHOLE_EXCLUDE_PREFIXES="XF:27ID1-BI{Kinetix-Det:1} XF:27ID1-ES{PANDA:1} XF:27IDF-OP:1{MC:5-" \
            nohup pixi run --manifest-path "$PROFILE_MANIFEST" -e terminal \
            python "$root/iocs/sim_ioc.py" --kinetix-ids 3 \
            > /tmp/hex-simioc.log 2>&1 &
        for _ in $(seq 30); do grep -q "serving" /tmp/hex-simioc.log 2>/dev/null && break; sleep 3; done
    else
        say "WARN: $PROFILE_MANIFEST not found — sim_ioc (shutters/metadata) NOT started."
    fi
fi
say "  sim_ioc OK"

# ---- 7. seed shutter status open (blackhole PVs are writable) -----------------
EPICS_CA_AUTO_ADDR_LIST=NO EPICS_CA_ADDR_LIST=127.0.0.1:5064 "$PY" - <<'EOF' >/dev/null 2>&1 || true
from epics import caput
for pv in ('XF:27IDA-PPS{Sh:FE}Sts:OpnCmd-Sts', 'XF:27IDA-PPS{L1-S1}Sts:OpnCmd-Sts'):
    caput(pv, 1, wait=True, timeout=5)
EOF
say "  shutters seeded open"

say "ALL UP. Client env: source scripts/env.sh"
say "Smoke test:   $PY iocs/panda/tests/slowmove_capture_test.py"
say "Boot profile: HEX_SIM=1 pixi run -e terminal ipython -- --profile-dir=.   (in hex-profile-collection)"
say "Oracle:       see oracle/Dockerfile header (or PROGRESS.md step 9)"
