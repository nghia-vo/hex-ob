"""
Structural mock test for lib.detectors + the alignment_scan plan (no EPICS).

Verifies, against ophyd-async 0.19.x with mock signal backends:
  1. make_kinetix builds and mock-connects (device tree is API-correct);
  2. stage -> prepare(TriggerInfo) drives the driver signals we expect
     (exposure, internal trigger, one frame per trigger);
  3. unstage restores the live-view (Continuous / Internal / acquiring);
  4. the SettablePathProvider retargeting used by alignment_scan works.

The functional run (frames actually captured, HDF file written, event
streams) happens against the simulated HEX beamline —
see tests/alignment_scan_sim_test.py.

Run from the hex-ob root (same path CI uses):
    pixi run test-mock
Or, from the profile env:
    cd hex-profile-collection && \
    PYTHONPATH=.. pixi run -e terminal python ../tests/alignment_scan_mock_test.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ophyd_async.core import TriggerInfo, init_devices

try:
    from ophyd_async.testing import set_mock_value
except ImportError:
    # ophyd_async.testing imports pytest at import time; the profile pixi env
    # has no pytest, so fall back to the symbol's real home there.
    from ophyd_async.core._mock_signal_utils import set_mock_value
from ophyd_async.epics.adcore import ADImageMode
from ophyd_async.epics.adkinetix import KinetixTriggerMode

from lib.detectors import SettablePathProvider, make_kinetix
from plans.tomography import alignment_scan  # noqa: F401  (import must succeed)


async def main() -> None:
    async with init_devices(mock=True):
        det = make_kinetix(1)

    # -- 1. structure -------------------------------------------------------
    assert det.name == "kinetix1"
    assert hasattr(det, "driver"), "driver sub-device missing"
    assert hasattr(det, "hdf"), "hdf writer (writer_name='hdf') missing"
    assert isinstance(det.path_provider, SettablePathProvider)
    print("PASS  device structure (driver / hdf / path_provider)")

    # -- 2. path provider retargeting (what alignment_scan does) ------------
    det.path_provider.set("/tmp/hex-ob-mock/scan_00001", "proj")
    info = det.path_provider()
    assert info.directory_path == Path("/tmp/hex-ob-mock/scan_00001")
    assert info.filename == "proj"
    print("PASS  SettablePathProvider retarget")

    # -- 3. stage -> prepare drives the driver ------------------------------
    # Directory creation is IOC-side (create_directory PV); in mock mode the
    # file_path_exists readback must be seeded or prepare refuses the path.
    set_mock_value(det.hdf.file_path_exists, True)
    await det.stage()
    await det.prepare(TriggerInfo(livetime=0.02, deadtime=0.002))
    assert await det.driver.acquire_time.get_value() == 0.02
    assert await det.driver.trigger_mode.get_value() == KinetixTriggerMode.INTERNAL
    assert await det.driver.image_mode.get_value() == ADImageMode.MULTIPLE
    assert await det.driver.num_images.get_value() == 1
    assert await det.hdf.swmr_mode.get_value() is False
    print("PASS  prepare(TriggerInfo) -> exposure/trigger/image-mode/no-SWMR")

    # -- 4. unstage restores live view --------------------------------------
    await det.unstage()
    assert await det.driver.trigger_mode.get_value() == KinetixTriggerMode.INTERNAL
    assert await det.driver.image_mode.get_value() == ADImageMode.CONTINUOUS
    assert await det.driver.acquire.get_value() is True
    print("PASS  unstage -> live-view restored (Continuous/Internal/acquiring)")

    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
