#!/usr/bin/env python3
"""Blackhole check: spoof_beamline fabricates plausibly-typed PVs on demand.

Self-contained: launches the vendored blackhole on a private CA port (:5099),
fetches a few PVs of different fabrication classes, checks the values match
what the acquisition stack expects, then tears it down. No other sim pieces
needed.

Run (env with `pyepics` + `caproto`): python tests/blackhole_fabrication_test.py
Exit code 0 = PASS.
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from localguard import assert_local_epics  # noqa: E402

PORT = 5099
os.environ["EPICS_CA_ADDR_LIST"] = "127.0.0.1:%d" % PORT
assert_local_epics()

HERE = os.path.dirname(os.path.abspath(__file__))
BLACKHOLE = os.path.join(HERE, "..", "..", "blackhole", "spoof_beamline.py")

EXPECT = [
    ("XF:27ID1-BI{Kinetix-Det:1}HDF1:PluginType_RBV", "NDFileHDF5"),
    ("XF:27ID1-BI{Kinetix-Det:1}cam1:ImageMode", "Single"),
    ("XF:27IDF-OP:1{Fake:Mtr}Mtr.RBV", None),  # fabricated numeric, any value
]

# Legacy ophyd's validate_asyn_ports needs every plugin's NDArrayPort value to
# name an existing driver PortName — so ALL fabricated port PVs must agree on
# one consistent port string (per-PV-name fabrication fails validation).
ASYN_PORT_PVS = [
    "XF:27ID1-BI{Kinetix-Det:1}cam1:PortName_RBV",
    "XF:27ID1-BI{Kinetix-Det:1}HDF1:NDArrayPort",
    "XF:27ID1-BI{Kinetix-Det:1}Stats1:NDArrayPort_RBV",
]


def main():
    # caproto's server keys off EPICS_CA_SERVER_PORT; set both like motor_ioc.
    env = dict(os.environ,
               EPICS_CAS_SERVER_PORT=str(PORT),
               EPICS_CA_SERVER_PORT=str(PORT))
    proc = subprocess.Popen(
        [sys.executable, BLACKHOLE],
        env=env, stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # spoof_beamline prints its blast-radius warning and waits for Enter.
    proc.stdin.write(b"\n")
    proc.stdin.flush()
    time.sleep(3)
    failures = []
    try:
        from epics import caget
        for pv, want in EXPECT:
            got = caget(pv, as_string=want is not None, timeout=5)
            print("%-50s -> %r" % (pv, got))
            if got is None or (want is not None and got != want):
                failures.append(pv)
        ports = [caget(pv, as_string=True, timeout=5) for pv in ASYN_PORT_PVS]
        for pv, got in zip(ASYN_PORT_PVS, ports):
            print("%-50s -> %r" % (pv, got))
        if None in ports or len(set(ports)) != 1:
            print("asyn ports inconsistent (validate_asyn_ports would fail)")
            failures.append("asyn-port-consistency")
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    if not failures:
        print("PASS")
        return 0
    print("FAIL:", failures)
    return 1


if __name__ == "__main__":
    sys.exit(main())
