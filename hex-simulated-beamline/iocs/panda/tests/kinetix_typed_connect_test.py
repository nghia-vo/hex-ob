#!/usr/bin/env python3
"""Kinetix personality test: the TYPED device connects to the FRAME tier.

Det:1 is a real AreaDetector IOC (ADSimDetector — real frames, production HDF
plugin). Historically the typed ophyd-async ``KinetixDetector`` refused to
connect to it (generic TriggerMode enum; Kinetix-specific PVs absent). The
Kinetix personality closes that: ``init_kinetix.py`` extends the TriggerMode
mbbo states on the real IOC, and ``sim_ioc --kinetix-overlay-ids 1`` serves
the Kinetix-only PVs (cam1:ReadoutPortIdx) the real IOC lacks — conflict-free.

This test connects the typed device FOR REAL (no mock) against Det:1 and
exercises one typed read and one typed round-trip write.

Run in the hex-profile-collection `terminal` pixi env (needs ophyd-async):
    pixi run -e terminal python iocs/panda/tests/kinetix_typed_connect_test.py
Exit code 0 = PASS.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "sim_devices"))
from localguard import assert_local_epics  # noqa: E402

# Det:1 (real IOC :5085) + the overlay/blackhole server (:5064).
assert_local_epics(default_ca="127.0.0.1:5064 127.0.0.1:5085")

from kinetix_sim import build_kinetix  # noqa: E402
from ophyd_async.core import init_devices  # noqa: E402
from ophyd_async.epics.adkinetix import KinetixTriggerMode, KinetixReadoutMode  # noqa: E402


async def main():
    async with init_devices():
        det = build_kinetix("XF:27ID1-BI{Kinetix-Det:1}")
    print("typed connect: OK (all signals)")

    tm = await det.driver.trigger_mode.get_value()
    assert tm == KinetixTriggerMode.INTERNAL, tm
    print("trigger_mode reads typed:", tm)

    await det.driver.readout_port_idx.set(KinetixReadoutMode.SPEED)
    rb = await det.driver.readout_port_idx.get_value()
    assert rb == KinetixReadoutMode.SPEED, rb
    print("readout_port_idx round-trip:", rb)
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
