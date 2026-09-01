import math
import asyncio
from ophyd_async.core import AsyncMovable, AsyncStatus, StandardReadable, init_devices, StandardMovable
from bluesky.callbacks.best_effort import BestEffortCallback
from ophyd_async.epics.motor import Motor
from bluesky import RunEngine
from bluesky.plan_stubs import rd, mv
from bluesky.plans import scan


# Example to show how to move 2 motors simultaneously

class MoveTwoMotor(StandardMovable):
    """
    Allow to move 2 motors (x, y) given a single input (r).
    """
    def __init__(self, name: str = ""):
        self.x = Motor("XF:27IDF-OP:1{SMPL:1-Ax:X1}Mtr")
        self.y = Motor("XF:27IDF-OP:1{SMPL:1-Ax:Z1}Mtr")
        super().__init__(name=name)

    @staticmethod
    def r_to_xy(r: float, theta: float = 45.0):
        """
        Convert r to x and y
        """
        theta_rad = math.radians(theta)
        x_val = r * math.sin(theta_rad)
        y_val = r * math.cos(theta_rad)
        return x_val, y_val

    @staticmethod
    async def _check_within_limits(motor: Motor, target: float) -> None:
        """Raise ValueError if ``target`` is outside the motor's soft limits."""
        low, high = await asyncio.gather(
            motor.low_limit_travel.get_value(),
            motor.high_limit_travel.get_value(),
        )
        # EPICS treats 0,0 as "no limit".
        if (low, high) != (0.0, 0.0) and not low <= target <= high:
            raise ValueError(
                f"{motor.name}: target {target} outside limits [{low}, {high}]"
            )

    @AsyncStatus.wrap
    async def set(self, r: float):
        """Move X and Y so both axes start and finish together.

        Demonstrates using the ``Motor`` signals: reads current positions and
        ``max_velocity`` to scale each axis' ``velocity`` for simultaneous
        arrival, checks soft limits first, and restores the original
        velocities afterwards.
        """
        x_target, y_target = self.r_to_xy(r)

        # 1. Reject the move up front if either target is out of bounds.
        await asyncio.gather(
            self._check_within_limits(self.x, x_target),
            self._check_within_limits(self.y, y_target),
        )

        # 2. Read current state needed to synchronize arrival.
        x_pos, y_pos, x_vmax, y_vmax, x_v0, y_v0 = await asyncio.gather(
            self.x.user_readback.get_value(),
            self.y.user_readback.get_value(),
            self.x.max_velocity.get_value(),
            self.y.max_velocity.get_value(),
            self.x.velocity.get_value(),
            self.y.velocity.get_value(),
        )

        x_dist = abs(x_target - x_pos)
        y_dist = abs(y_target - y_pos)

        # 3. Drive the longer travel at its max velocity; scale the other axis
        #    so both take the same amount of time to arrive.
        # move_time = max(x_dist / x_vmax, y_dist / y_vmax) if max(x_dist, y_dist) else 0.0
        # if move_time > 0.001:
        #     x_velo = x_dist / move_time if x_dist else x_v0
        #     y_velo = y_dist / move_time if y_dist else y_v0
        #     await asyncio.gather(
        #         self.x.velocity.set(x_velo),
        #         self.y.velocity.set(y_velo),
        #     )

        

        # 4. Move both axes, then restore the original velocities.
        try:
            await asyncio.gather(self.x.set(x_target), self.y.set(y_target))
        finally:
            await asyncio.gather(
                self.x.velocity.set(x_v0),
                self.y.velocity.set(y_v0),
            )

RE = RunEngine({})
with init_devices():
    comb_stage = MoveTwoMotor(name="comb_stage")

RE(mv(comb_stage, 5))
# bec = BestEffortCallback()
# RE.subscribe(bec)
# RE(scan([], comb_stage, 0, 5, 2))
RE(mv(comb_stage, 0))
