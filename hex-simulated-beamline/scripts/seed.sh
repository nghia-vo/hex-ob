#!/usr/bin/env bash
# Seed the local stack: Redis cycle/data_session keys + a Tiled hex/raw catalog
# container (CatalogOfBlueskyRuns). Re-runnable; the --temp Tiled catalog resets
# on restart so this must run after every `up`.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"
compose="docker compose -f $root/compose/docker-compose.yml"

echo "[seed] experiment identity (sim sync-experiment)..."
"$here/sync_sim_experiment.sh"

echo "[seed] Tiled hex/raw container..."
# Run inside the tiled container (localhost == the server there).
$compose exec -T tiled python -c "
from tiled.client import from_uri
c = from_uri('http://127.0.0.1:8000', api_key='secret')
top = c.create_container('hex') if 'hex' not in c else c['hex']
if 'raw' not in top:
    top.create_container('raw', specs=['CatalogOfBlueskyRuns'])
print('tiled containers:', list(c), '->', list(c['hex']))
"
echo "[seed] done."
