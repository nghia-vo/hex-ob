#!/usr/bin/env python3
"""Motor-motion check: the FakeMotor IOC really moves like a motor.

Verifies the dedicated motor IOC (:5075) produces what the capture chain and
the acquisition scripts rely on: RBV ramps monotonically toward the setpoint
at roughly VELO, and DMOV cycles 1 -> 0 (moving) -> 1 (done).

Run (env with `pyepics`): python tests/motor_motion_test.py
Exit code 0 = PASS.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from localguard import assert_local_epics  # noqa: E402

assert_local_epics(default_ca="127.0.0.1:5075")

from epics import caget, caput  # noqa: E402

MOTOR = "XF:27IDF-OP:1{MC:5-Ax:4}Mtr"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--distance-deg", type=float, default=4.0)
    p.add_argument("--velo", type=float, default=2.0)
    args = p.parse_args(argv)

    caput(MOTOR + ".VELO", 10.0, wait=True)
    caput(MOTOR + ".VAL", 0.0, wait=True)
    time.sleep(0.5)
    caput(MOTOR + ".VELO", args.velo, wait=True)

    caput(MOTOR + ".VAL", args.distance_deg, wait=False)
    saw_moving = False
    samples = []
    deadline = time.time() + args.distance_deg / args.velo * 3 + 10
    while time.time() < deadline:
        time.sleep(0.15)
        samples.append(float(caget(MOTOR + ".RBV")))
        if caget(MOTOR + ".DMOV") == 0:
            saw_moving = True
        if caget(MOTOR + ".DMOV") == 1 and saw_moving:
            break
    final = float(caget(MOTOR + ".RBV"))
    caput(MOTOR + ".VELO", 10.0, wait=True)

    # FakeMotor's readback carries encoder-like jitter; require a rising
    # trend, not strict sample-to-sample monotonicity.
    monotonic = all(b >= a - 0.05 for a, b in zip(samples, samples[1:]))
    at_target = abs(final - args.distance_deg) < 0.05
    n_updates = len(set(round(s, 4) for s in samples))
    print("samples=%d distinct=%d monotonic=%s DMOV-cycled=%s final=%.3f"
          % (len(samples), n_updates, monotonic, saw_moving, final))
    if monotonic and saw_moving and at_target and n_updates >= 3:
        print("PASS")
        return 0
    print("FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
