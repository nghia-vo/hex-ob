#!/usr/bin/env python3
"""One-time init for the sim pandablocks-ioc (autosave-equivalents).

On the real beamline these IOC-level settings persist; a recreated sim
container starts bare. Apply after any panda-ioc restart (the panda-side
block design is separate — ``hex_tomo_design.py`` over the control port):

  * ``CALC2:OUT:DATASET = "Angle"`` — names the captured angle dataset in
    panda.hdf, which is exactly what the pyepics scripts read back via
    ``losa.load_hdf(panda.hdf, "Angle")``.

Run (env with `pyepics`): python iocs/panda/init_panda_ioc.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from localguard import assert_local_epics  # noqa: E402

assert_local_epics(default_ca="127.0.0.1:5095")

from epics import caget, caput  # noqa: E402

P = "XF:27ID1-ES{PANDA:1}:"

SETTINGS = [
    ("CALC2:OUT:DATASET", "Angle"),
]


def main():
    failures = 0
    for pv, value in SETTINGS:
        ok = caput(P + pv, value, wait=True, timeout=5)
        got = caget(P + pv, as_string=True, timeout=5)
        print("%-22s = %-8r -> readback %r" % (pv, value, got))
        if ok is None or got != value:
            failures += 1
    if failures:
        print("FAILED to apply %d setting(s)" % failures, file=sys.stderr)
        return 1
    print("panda-ioc initialized.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
