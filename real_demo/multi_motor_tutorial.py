import math
import asyncio
from ophyd_async.core import AsyncMovable, AsyncStatus, StandardReadable, init_devices, StandardMovable
from bluesky.callbacks.best_effort import BestEffortCallback
from ophyd_async.epics.motor import Motor
from bluesky import RunEngine
from bluesky.plan_stubs import rd, mv
from bluesky.plans import scan


# Example to show how to move 2 motors simultaneously

class MoveTwoMotor(StandardReadable):
    """
    Allow to move 2 motors (x, y) given a single input (r).
    """
    def __init__(self, name: str = ""):
        self.x = Motor("XF:27IDF-OP:1{SMPL:1-Ax:X1}Mtr")
        self.y = Motor("XF:27IDF-OP:1{SMPL:1-Ax:Z1}Mtr")
        super().__init__(name=name)

    @staticmethod
    def r_to_xy(r: float):
        """
        Convert r to x and y
        """
        theta = 45.0  # degree
        theta_rad = math.radians(theta)
        x_val = r * math.sin(theta_rad)
        y_val = r * math.cos(theta_rad)
        return x_val, y_val

    @AsyncStatus.wrap
    async def set(self, r: float):
        """Move X-motor and Y-motor given r value (all motors simultaneously)."""
        x_val, y_val = self.r_to_xy(r)
        await asyncio.gather(self.x.set(x_val), self.y.set(y_val))

RE = RunEngine({})
with init_devices():
    comb_stage = MoveTwoMotor(name="comb_stage")

RE(mv(comb_stage, 5))
# bec = BestEffortCallback()
# RE.subscribe(bec)
# RE(scan([], comb_stage, 0, 5, 2))

