import asyncio
from dataclasses import dataclass
from functools import cached_property

from bluesky import RunEngine
from bluesky.plan_stubs import mv
from bluesky.plans import scan
from bluesky.callbacks.best_effort import BestEffortCallback

from ophyd_async.epics.motor import Motor
from ophyd_async.core import (MovableLogic, StandardMovable, StandardReadable, TimeoutCalculator, init_devices,
                              soft_signal_r_and_setter, soft_signal_rw)
from ophyd_async.core import StandardReadableFormat as Format


# Example to show how to write Ophyd-async for a compound motor made from 2 motors
# The key is that it needs a set of methods (move, check_move, stop) to be treat like a single motor from Bluesky requirement.

@dataclass
class MoveTwoMotorLogic(MovableLogic[float]):
    """Movement logic for two motors acting as one compound axis."""
    x: Motor
    y: Motor

    async def check_move(self, new_position: float) -> None:
        """
        Check that both physical motors can move to the requested position.
        """
        await asyncio.gather(self.x.check_value(new_position),
                             self.y.check_value(new_position))

    async def calculate_timeout(self, old_position: float, new_position: float) -> float | None:
        """
        Calculate timeout based on the actual positions of both motors.

        """
        del old_position

        x_position, y_position = await asyncio.gather(self.x.movable_logic.readback.get_value(),
                                                      self.y.movable_logic.readback.get_value())

        x_timeout, y_timeout = await asyncio.gather(
            self.x.movable_logic.calculate_timeout(x_position, new_position),
            self.y.movable_logic.calculate_timeout(y_position, new_position,))

        timeouts = [t for t in (x_timeout, y_timeout)if t is not None]

        return max(timeouts) if timeouts else None

    async def move(self, new_position: float, timeout: TimeoutCalculator) -> None:
        """
        Move both physical motors simultaneously.
        """

        # Set the requested logical position.
        await self.setpoint.set(new_position)

        # Get the remaining timeout calculated by StandardMovable.
        move_timeout = timeout()

        # Move both motors concurrently.
        await asyncio.gather(self.x.set(new_position, timeout=move_timeout,),
                             self.y.set(new_position, timeout=move_timeout))

    async def stop(self) -> None:
        """
        Stop both physical motors simultaneously.
        """
        await asyncio.gather(self.x.stop(), self.y.stop())


class MoveTwoMotor(StandardReadable, StandardMovable[float]):
    """
    Two physical motors behaving as one logical translation stage.
    """

    def __init__(self, name: str = ""):
        self.x = Motor("XF:27IDF-OP:1{SMPL:1-Ax:X2}Mtr")
        self.y = Motor("XF:27IDF-OP:1{SMPL:1-Ax:Z2}Mtr")

        self.setpoint = soft_signal_rw(float, float("nan"), units="mm")

        with self.add_children_as_readables(Format.HINTED_SIGNAL):
            self.readback, _ = soft_signal_r_and_setter(float, units="mm",
                                                        getter=self._get_position, poll_period=0.1)

        super().__init__(name=name)

    async def _get_position(self) -> float:
        x, y = await asyncio.gather(self.x.movable_logic.readback.get_value(),
                                    self.y.movable_logic.readback.get_value())
        return (x + y) / 2

    @cached_property
    def movable_logic(self) -> MovableLogic[float]:
        return MoveTwoMotorLogic(setpoint=self.setpoint,
                                 readback=self.readback, x=self.x, y=self.y)


RE = RunEngine({})

with init_devices():
    comb_stage = MoveTwoMotor(name="comb_stage")
    x = Motor("XF:27IDF-OP:1{SMPL:1-Ax:X2}Mtr", name="x")


RE(mv(comb_stage, 0.0)) # Move the compoud stage to 0.0

bec = BestEffortCallback()
RE.subscribe(bec) # To display the positions and time-stamp
RE(scan([], comb_stage, 0, 5, 6)) # No detector used in scan method
