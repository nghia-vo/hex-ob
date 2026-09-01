# Basic examples on how to define a single motor, get its values (position, velocity, ...)
# and move it.

from bluesky import RunEngine
from ophyd_async.core import init_devices
from ophyd_async.epics.motor import Motor
from bluesky.plan_stubs import rd, mv
from bluesky.callbacks.best_effort import BestEffortCallback
from bluesky.plans import scan
from bluesky.utils import ProgressBarManager


# 1st way of geting motor values
# Initialize a run engine
RE = RunEngine({}, call_returns_result=True) # Or simply RE = RunEngine() if we don't need return result

# Initialize a device, need to after RE initilization
with init_devices():
    x = Motor("XF:27IDF-OP:1{SMPL:1-Ax:X1}Mtr", name="x")

# Get properties and methods of the motor. This is handy if you want to get its info such as velocity, current position, ...
for p in dir(x):
    if "__" not in p:
        print("->: ", p)
print("==================================")
print("First way of getting motor values")
# Get current position
result = RE(rd(x))
print('Position: ', result.plan_result)
# Get current velocity
result = RE(rd(x.velocity))
print('Velocity: ', result.plan_result)
# Get current limits
result = RE(rd(x.low_limit_travel))
print('Low-limit: ', result.plan_result)
result = RE(rd(x.high_limit_travel))
print('High-limit: ', result.plan_result)

# 2nd way of geting motor values where we define a custom plan
print("==================================")
print("Second way of getting motor values")
# Get current position
def get_position(x):
    position = yield from rd(x)
    print("Position: ", position)
RE(get_position(x))
# Get current velocity
def get_velocity(x):
    velocity = yield from rd(x.velocity)
    print("Velocity: ", velocity)
RE(get_velocity(x))
# Get limits
def get_limits(x):
    low_limit = yield from rd(x.low_limit_travel)
    print("Low-limit: ", low_limit)
    high_limit = yield from rd(x.high_limit_travel)
    print("High-limit: ", high_limit)
RE(get_limits(x))
print("============================")
print("Move motor to 5 mm ...")
# Move motor to new position = 5 mm
RE(mv(x, 5)) # move to 5 mm
print("Done!\n")

print("============================")
print("Move motor to 0.0 mm and display progress bar\n")
# Hook progress bar to check the movement status
RE.waiting_hook = ProgressBarManager()
RE(mv(x, 0.0)) # move to 0.0 mm
print("Done\n")

print("============================")
print("Move motor from 0.0 -> 5 mm with the step of 1mm \n")
# Move the stage from 0 -> 5 with the step of 1 mm
RE.waiting_hook = None
bec = BestEffortCallback()
RE.subscribe(bec) # To display the positions and time stamp
RE(scan([], x, 0, 5, 6)) # No detector used in scan method
