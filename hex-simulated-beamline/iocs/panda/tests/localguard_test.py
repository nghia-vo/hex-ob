#!/usr/bin/env python3
"""Beamline-guard check: sim EPICS clients refuse non-loopback networks.

Proves the safeguard behind the sim's beamline-safety requirement: a client
process whose EPICS address list points at anything that could be a beamline
subnet must refuse to start, and a loopback-only environment must pass.
Exercises `localguard.assert_local_epics` in subprocesses (it exits the
process on violation) — including through the real motor bridge entry point.

Run: python tests/localguard_test.py   (no sim services needed)
Exit code 0 = PASS.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PANDA_DIR = os.path.join(HERE, "..")
BRIDGE = os.path.join(PANDA_DIR, "motor_encoder_bridge.py")

SNIPPET = (
    "import sys; sys.path.insert(0, %r); "
    "from localguard import assert_local_epics; assert_local_epics(); "
    "print('STARTED')" % PANDA_DIR
)


def run_case(name, env_extra, argv, expect_refusal):
    env = dict(os.environ)
    env.pop("EPICS_CA_ADDR_LIST", None)
    env.pop("EPICS_PVA_ADDR_LIST", None)
    env.update(env_extra)
    r = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
    refused = r.returncode != 0 and "localguard: REFUSING" in (r.stderr + r.stdout)
    ok = refused if expect_refusal else (not refused)
    print("%-38s -> %s (rc=%d)" % (name, "ok" if ok else "WRONG", r.returncode))
    return ok


def main():
    py = sys.executable
    cases = [
        # A beamline-subnet-looking address must be refused...
        ("guard: beamline addr refused",
         {"EPICS_CA_ADDR_LIST": "10.68.27.11"}, [py, "-c", SNIPPET], True),
        # ...even mixed in among loopback entries...
        ("guard: mixed addrs refused",
         {"EPICS_CA_ADDR_LIST": "127.0.0.1 xf27id1-ioc1"},
         [py, "-c", SNIPPET], True),
        # ...and non-loopback PVA is refused too.
        ("guard: PVA addr refused",
         {"EPICS_CA_ADDR_LIST": "127.0.0.1", "EPICS_PVA_ADDR_LIST": "10.68.27.11"},
         [py, "-c", SNIPPET], True),
        # Loopback-only passes.
        ("guard: loopback passes",
         {"EPICS_CA_ADDR_LIST": "127.0.0.1 127.0.0.1:5075"},
         [py, "-c", SNIPPET], False),
        # The real bridge entry point refuses before touching CA.
        ("bridge: beamline addr refused",
         {"EPICS_CA_ADDR_LIST": "10.68.27.11"},
         [py, BRIDGE, "--connect-timeout", "1"], True),
    ]
    results = [run_case(*c) for c in cases]
    if all(results):
        print("PASS")
        return 0
    print("FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
