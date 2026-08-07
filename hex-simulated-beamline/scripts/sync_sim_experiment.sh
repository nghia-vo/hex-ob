#!/usr/bin/env bash
# Simulated sync-experiment: mimic nslsii.sync_experiment.switch_redis_proposal
# for the HEX sim — the PROCESS is reproduced, the IDENTITY is unmistakably fake.
#
# The real flow (nslsii/sync_experiment/sync_experiment.py):
#   authenticate (LDAP) -> authorize (/v1/data-session/<user>) -> validate the
#   proposal against the PASS API (pass-NNNNNN, current cycle, beamline) ->
#   write the experiment identity into the beamline Redis (RedisJSONDict):
#   data_session, username, start_datetime, tiled_access_tags, cycle,
#   proposal{proposal_id,title,type,pi_name}.
#
# Sim mapping (decision AJ + agent, 2026-07-30):
#   * auth/authz/PASS validation are SKIPPED BY DESIGN (no facility services in
#     the sim) — everything else writes the same keys with the same shapes.
#   * data_session is the reserved sentinel "pass-000000" — not a plausible
#     PASS number, and the proposal metadata says type=SIMULATED loudly.
#   * The facility-provisioned storage tree is mimicked UNDER THE SIM DATA
#     ROOT ONLY: /tmp/hex-sim-data/nsls2/data/hex/proposals/<cycle>/... —
#     NEVER a real /nsls2 path on the host, never a real proposal number.
#     Containers that must see "/nsls2" bind-mount this root there.
#
# Idempotent; run after `up.sh` (seed.sh calls it).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"
compose="docker compose -f $root/compose/docker-compose.yml"

CYCLE="${HEX_SIM_CYCLE:-2026-2}"
DATA_SESSION="pass-000000"
SIM_USER="${USER:-simuser}"
SIM_NSLS2_ROOT="${HEX_SIM_NSLS2_ROOT:-/tmp/hex-sim-data/nsls2}"

rcli="redis-cli -p 6380 --tls --cert /certs/redis.crt --key /certs/redis.key --cacert /certs/redis.crt"

echo "[sync-sim] experiment identity -> redis (data_session=$DATA_SESSION cycle=$CYCLE)"
$compose exec -T redis $rcli set data_session "\"$DATA_SESSION\"" >/dev/null
$compose exec -T redis $rcli set username "\"$SIM_USER\"" >/dev/null
$compose exec -T redis $rcli set start_datetime "\"$(date -Iseconds)\"" >/dev/null
$compose exec -T redis $rcli set cycle "\"$CYCLE\"" >/dev/null
$compose exec -T redis $rcli set tiled_access_tags "[\"$DATA_SESSION\"]" >/dev/null
$compose exec -T redis $rcli set proposal \
  "{\"proposal_id\": \"000000\", \"title\": \"SIMULATED HEX beamline development (not a real proposal)\", \"type\": \"SIMULATED\", \"pi_name\": \"$SIM_USER\"}" >/dev/null

echo "[sync-sim] sim storage tree (facility-provisioning mimic) -> $SIM_NSLS2_ROOT"
proposal_dir="$SIM_NSLS2_ROOT/data/hex/proposals/$CYCLE/$DATA_SESSION"
mkdir -p "$proposal_dir/tomography/raw_data" "$proposal_dir/tomography/tmp"
cat > "$SIM_NSLS2_ROOT/_SIMULATED_DATA_README" <<EOF
EVERYTHING under this tree is SIMULATED-BEAMLINE output (data_session
$DATA_SESSION). It mimics the /nsls2/data layout so acquisition scripts run
unmodified inside containers that bind-mount this root at /nsls2. Nothing
here is real experiment data; folder names that look like real proposals
(created verbatim by legacy scripts) are simulation artifacts.
EOF
# Tolerant: container-written scan outputs (other UIDs) are already usable.
chmod -R a+rwX "$SIM_NSLS2_ROOT" 2>/dev/null || true

# Storage guard: sim data is disposable, but a runaway high-fidelity scan can
# be huge (real-Kinetix geometry is ~20 MB/frame -> ~36 GB per 1801-proj scan).
usage_gb=$(du -s --block-size=1G /tmp/hex-sim-data 2>/dev/null | cut -f1 || echo 0)
if [ "${usage_gb:-0}" -ge "${HEX_SIM_DATA_WARN_GB:-50}" ]; then
    echo "[sync-sim] WARNING: sim data at /tmp/hex-sim-data is ${usage_gb} GB" \
         "(warn threshold ${HEX_SIM_DATA_WARN_GB:-50} GB) — run scripts/prune_sim_data.sh --yes"
fi

echo "[sync-sim] done."
