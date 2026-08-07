#!/usr/bin/env python3
"""One-time init for the sim Kinetix AD IOC (the panda-design analog).

The real beamline IOC carries autosaved settings the pyepics scripts rely on
but never set themselves. A freshly deployed sim IOC starts bare, so apply
the equivalents here after `docker compose -f compose/docker-compose.kinetix.yml
up -d` (rerun after any container recreate):

  * ``cam1:ArrayCallbacks = 1`` — without it the driver produces frames but
    pushes none to the plugins, and ``HDF1:NumCaptured_RBV`` never moves.
  * plugin blocking callbacks left default; add future autosave-equivalents
    here as they surface.

Run (env with `pyepics`): python iocs/kinetix/init_kinetix.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "panda"))
from localguard import assert_local_epics  # noqa: E402

assert_local_epics(default_ca="127.0.0.1:5085")

from epics import caget, caput  # noqa: E402

P = "XF:27ID1-BI{Kinetix-Det:1}"

SETTINGS = [
    ("cam1:ArrayCallbacks", 1),
    # Kinetix personality (chg:kinetix-personality): the typed KinetixDetector
    # demands TriggerMode choices {Internal, Rising Edge, Exp. Gate} (ophyd-async
    # compares as a SET). ADSimDetector's mbbo ships Internal/External — relabel
    # state 1 and add state 2 at runtime. In the sim the driver ignores the
    # semantic difference (frames come from Acquire/ImageMode, not triggers).
    ("cam1:TriggerMode.ONST", "Rising Edge"),
    ("cam1:TriggerMode.TWST", "Exp. Gate"),
    ("cam1:TriggerMode_RBV.ONST", "Rising Edge"),
    ("cam1:TriggerMode_RBV.TWST", "Exp. Gate"),
]


def main():
    failures = 0
    for pv, value in SETTINGS:
        ok = caput(P + pv, value, wait=True, timeout=5)
        got = caget(P + pv, timeout=5)
        print("%-24s = %-6s -> readback %s" % (pv, value, got))
        if ok is None or got is None:
            failures += 1
    if failures:
        print("FAILED to apply %d setting(s)" % failures, file=sys.stderr)
        return 1
    print("Kinetix sim IOC initialized.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
