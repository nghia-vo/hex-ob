#!/usr/bin/env bash
# Start the PV "blackhole" IOC for the simulated HEX beamline.
#
# The blackhole (vendored verbatim from NSLS2/test-beamline-profiles/spoof_beamline.py)
# fabricates any requested PV with a plausible default — enough to let the HEX
# profile start and ophyd/ophyd-async objects connect. NOTE: values are STATIC —
# no motor motion or frame acquisition (that's a later phase, via containerized
# IOCs from nsls2.ioc_deploy). Binds to 127.0.0.1 only.
#
# Requires caproto in the active environment (`pip install caproto`). Run in its
# own terminal; Ctrl-C to stop.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"

# shellcheck source=/dev/null
source "$root/scripts/env.sh"

if ! python -c "import caproto" >/dev/null 2>&1; then
    echo "ERROR: caproto not importable in this env. Try: pip install caproto" >&2
    exit 1
fi

# A CA repeater keeps ophyd clients from complaining; start one if available.
if command -v caproto-repeater >/dev/null 2>&1; then
    caproto-repeater --quiet >/dev/null 2>&1 &
fi

echo "[blackhole] starting PV blackhole on 127.0.0.1 (Ctrl-C to stop)..."
# Feed a newline to satisfy the script's acknowledgement prompt.
echo "" | exec python "$here/spoof_beamline.py"
