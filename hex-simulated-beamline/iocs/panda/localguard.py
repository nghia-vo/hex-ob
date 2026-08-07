"""Beamline guard: refuse to run sim-side EPICS clients unless loopback-only.

The HEX sim deliberately reuses REAL beamline PV names so the acquisition
scripts run unmodified. That means any sim-side CA client (the motor->INENC
bridge, the capture tests — things that WRITE to motors) pointed at the wrong
network would address the real beamline. Decision (AJ, 2026-07-30, recorded as
the sim's beamline-safety requirement): enforce, don't trust configuration —
if the sim is ever run from a host on the HEX beamline subnets, it must be
impossible to inadvertently control real devices.

``assert_local_epics()`` therefore:

* forces ``EPICS_CA_AUTO_ADDR_LIST=NO`` / ``EPICS_PVA_AUTO_ADDR_LIST=NO``
  (no broadcast searching, ever), and
* requires every entry of ``EPICS_CA_ADDR_LIST`` / ``EPICS_PVA_ADDR_LIST``
  to be a loopback address (127.0.0.0/8 or ``localhost``), aborting loudly
  otherwise.

Call it BEFORE importing ``epics``/``p4p`` — those read the environment at
import time. Servers are out of scope here: the compose services bind
127.0.0.1 explicitly, and the caproto IOCs are started with loopback-only
client instructions in scripts/env.sh.

Aligned with the N3XTware FDR simulator safety guardrails ("hardware
isolation", "namespace safety").
"""

import ipaddress
import os
import sys


def _is_loopback(entry):
    host = entry.rsplit(":", 1)[0] if ":" in entry else entry
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # unresolvable / hostname: reject, loopback must be explicit


def assert_local_epics(default_ca="127.0.0.1 127.0.0.1:5075", default_pva="127.0.0.1"):
    """Force + validate a loopback-only EPICS client environment or exit(2)."""
    os.environ["EPICS_CA_AUTO_ADDR_LIST"] = "NO"
    os.environ["EPICS_PVA_AUTO_ADDR_LIST"] = "NO"
    ca = os.environ.get("EPICS_CA_ADDR_LIST") or default_ca
    pva = os.environ.get("EPICS_PVA_ADDR_LIST") or default_pva
    os.environ["EPICS_CA_ADDR_LIST"] = ca
    os.environ["EPICS_PVA_ADDR_LIST"] = pva
    bad = [e for e in (ca.split() + pva.split()) if not _is_loopback(e)]
    if bad:
        sys.exit(
            "localguard: REFUSING to start — non-loopback EPICS address(es) "
            "%s. The HEX sim uses REAL beamline PV names; running its clients "
            "against a beamline network could drive real motors/devices. Set "
            "EPICS_CA_ADDR_LIST/EPICS_PVA_ADDR_LIST to 127.0.0.1 entries only."
            % bad
        )
    return ca
