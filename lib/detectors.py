"""
HEX beamline Kinetix detector device definitions (ophyd-async).

Devices defined here:
    - kinetix1  : Kinetix camera #1  (XF:27ID1-BI{Kinetix-Det:1})
    - kinetix3  : Kinetix camera #3  (XF:27ID1-BI{Kinetix-Det:3})

The HEXKinetixDetector subclass resets the camera to Continuous / Internal
trigger mode after each scan so the live-view is restored automatically.
"""

import asyncio

from ophyd_async.core import AsyncStatus, StandardDetector
from ophyd_async.epics.adkinetix import KinetixDetector
from ophyd_async.epics.adcore import ADHDFWriter


class HEXADHDFWriter(ADHDFWriter):
    """Override to disable SWMR mode, which is not supported at HEX."""

    async def begin_capture(self):
        await super().begin_capture()
        await self.fileio.swmr_mode.set(False)


class HEXKinetixDetector(KinetixDetector):
    """
    HEX-specific Kinetix detector.

    After every scan (or abort) the camera is reset to Continuous / Internal
    mode so that the live-view image stream restarts automatically.
    """

    @AsyncStatus.wrap
    async def unstage(self) -> None:
        # Stop data writing and disarm the controller.
        await asyncio.gather(self._writer.close(), self._controller.disarm())

        # Return to continuous free-run mode for live viewing.
        await self.driver.trigger_mode.set("Internal")
        await self.driver.image_mode.set("Continuous")
        await self._controller.arm()


def make_kinetix(detector_id: int, path_provider) -> HEXKinetixDetector:
    """
    Instantiate a HEXKinetixDetector for the given detector number (1 or 3).

    Parameters
    ----------
    detector_id:
        Which Kinetix to connect to (1 or 3).
    path_provider:
        An ophyd-async PathProvider that decides where HDF files are written.
    """
    if detector_id not in (1, 3):
        raise ValueError(f"Kinetix detector id must be 1 or 3, got {detector_id}")

    return HEXKinetixDetector(
        f"XF:27ID1-BI{{Kinetix-Det:{detector_id}}}",
        path_provider,
        name=f"kinetix{detector_id}",
        writer_cls=HEXADHDFWriter,
    )
