"""Tutorial: build a raw ophyd-async EPICS device by hand.

Goal: learn the ``A[Signal, PvSuffix]`` annotation pattern used throughout
``lib/`` (see lib/phantom.py, lib/detectors.py) by wiring signals to real
motor-record PV fields ourselves, instead of reaching for the ready-made
``Motor``.

Key facts about the imports (verified against ophyd_async 0.13.4):
    - ``A`` is just ``typing.Annotated``.
    - ``EpicsDevice`` / ``PvSuffix`` live in ``ophyd_async.epics.core``.
    - the read/config formatting enum is ``StandardReadableFormat`` (aliased
      here to ``Format``), from ``ophyd_async.core``.
"""

import asyncio
from typing import Annotated as A

from bluesky import RunEngine
from ophyd_async.core import (
    AsyncStatus,
    SignalR,
    SignalRW,
    StandardReadable,
    StandardReadableFormat as Format,
    init_devices,
    AsyncMovable,
    wait_for_value,
)
from ophyd_async.epics.core import EpicsDevice, PvSuffix
from ophyd_async.epics.motor import Motor
from ophyd_async.plan_stubs import ensure_connected

#import ophyd_async.epics.core._aioca as _ophyd_aioca  # noqa: PLC2701


# FIXME:
# RunEngine imports pyepics. Reusing pyepics' CA context makes
# aioca/ophyd-async signal connections time out against caproto IOCs.
#_ophyd_aioca._use_pyepics_context_if_imported = lambda: None  # noqa: SLF001


# This class is already built in ophyd-async, so this is just for demonstration purposes
class MotorAxisIO(StandardReadable, AsyncMovable[float], EpicsDevice):
    """One motor axis, assembled from raw PV fields.

    A ``PvSuffix`` is appended to the device prefix. Giving a prefix of
    ``XF:27IDF-OP:1{SMPL:1-Ax:X1}Mtr`` therefore builds:
        ``.RBV`` -> read-back value (the true, measured position)
        ``.VAL`` -> set-point (the value you command it to move to)

    Inheriting ``StandardReadable`` *and* ``EpicsDevice`` lets us use both the
    ``PvSuffix`` wiring (from ``EpicsDevice``) and the ``Format`` read/config
    tagging (from ``StandardReadable``) on the same annotation.
    """

    readback: A[SignalR[float], PvSuffix(".RBV"), Format.HINTED_SIGNAL]
    setpoint: A[SignalRW[float], PvSuffix(".VAL"), Format.CONFIG_SIGNAL]
    velocity: A[SignalRW[float], PvSuffix(".VELO"), Format.CONFIG_SIGNAL]

    @AsyncStatus.wrap
    async def set(self, value: float) -> AsyncStatus:
        start_pos, velo = await asyncio.gather(
            self.readback.get_value(),
            self.velocity.get_value(),
        )
        time_for_move = abs(value - start_pos) / velo
        await self.setpoint.set(value)
        await wait_for_value(self.readback, value, timeout=time_for_move + 10)


RE = RunEngine()


# Hand-built raw devices (constructed unconnected — importing this module has
# no side effects and needs no running event loop).
with init_devices():
    x1_raw = MotorAxisIO("XF:27IDF-OP:1{SMPL:1-Ax:X1}Mtr", name="x1_raw")
    z1_raw = MotorAxisIO("XF:27IDF-OP:1{SMPL:1-Ax:Z1}Mtr", name="z1_raw")

    # Second way using ophyd-async Motor class directly
    # Ready-made equivalents for comparison: a `Motor` already bundles .RBV/.VAL
    # (and much more) so you normally use these in real plans.
    x1 = Motor("XF:27IDF-OP:1{SMPL:1-Ax:X1}Mtr", name="x1")
    z1 = Motor("XF:27IDF-OP:1{SMPL:1-Ax:Z1}Mtr", name="z1")



#RE(ensure_connected(x1_raw,z1_raw))





# if __name__ == "__main__":
#     # A RunEngine starts the bluesky event loop that device connection uses.
#     RE = RunEngine()

#     # `init_devices` connects everything constructed inside the block. Use
#     # mock=True to run anywhere; drop it (or set mock=False) on a machine with
#     # EPICS access to the real/sim beamline.
#     with init_devices(mock=True):
#         demo = MotorAxisIO("XF:27IDF-OP:1{SMPL:1-Ax:X1}Mtr", name="demo")

#     print("readback PV:", demo.readback.source)
#     print("setpoint PV:", demo.setpoint.source)

