#!/usr/bin/env bash
# Prune simulated detector/PandA output. ALL data under the sim root is
# disposable by design (sentinel data_session pass-000000, see
# sync_sim_experiment.sh) — this reclaims space while keeping the provisioned
# tree + marker README so the stack keeps working without a re-seed.
#
#   ./scripts/prune_sim_data.sh          # report sizes only
#   ./scripts/prune_sim_data.sh --yes    # delete scan outputs
set -euo pipefail

ROOT="${HEX_SIM_DATA_DIR:-/tmp/hex-sim-data}"
NSLS2_ROOT="${HEX_SIM_NSLS2_ROOT:-$ROOT/nsls2}"

echo "[prune] sim data usage:"
du -sh "$ROOT" 2>/dev/null || { echo "  (no sim data at $ROOT)"; exit 0; }
du -sh "$NSLS2_ROOT"/data/hex/proposals/*/* 2>/dev/null || true

if [ "${1:-}" != "--yes" ]; then
    echo "[prune] dry run — pass --yes to delete scan outputs (all sim data is disposable)."
    exit 0
fi

# Delete scan outputs but keep the provisioned skeleton + marker.
find "$NSLS2_ROOT/data" -mindepth 6 -depth -delete 2>/dev/null || true
find "$ROOT" -maxdepth 1 -type f \( -name "*.hdf" -o -name "*.h5" -o -name "*.tif" -o -name "*.tiff" \) -delete
echo "[prune] done:"
du -sh "$ROOT"
