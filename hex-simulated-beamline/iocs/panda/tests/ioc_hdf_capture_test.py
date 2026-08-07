#!/usr/bin/env python3
"""PandA sim capture test: the full IOC data path through the REAL design.

Same chain as slowmove_capture_test (motor -> bridge -> Tomo_radio_1_config
-> capture) but driven entirely through the published pandablocks-ioc PVs the
acquisition software uses, exactly like ``deco.Panda`` does in time mode:

  PCOMP1:START (start encoder angle) + PULSE1:STEP/WIDTH/PULSES (time train),
  CALC2:OUT:DATASET=Angle, DATA:* configured, DATA:Capture=1, PCAP:ARM=1,
  move the motor -- no BITS:A (arming is the gate in the real design).

Expect DATA:NumCaptured to reach PULSES and panda.hdf to contain an ``Angle``
dataset in DEGREES (the design carries the real scale 0.005 / offset 198.3).

Prereqs (PROGRESS.md bring-up): panda sim + panda-ioc containers, real tomo
design applied, motor IOC (:5075), bridge running.

Run (env with `pyepics` + `h5py`):
    python tests/ioc_hdf_capture_test.py
Exit code 0 = PASS.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from localguard import assert_local_epics  # noqa: E402

assert_local_epics(default_ca="127.0.0.1:5095 127.0.0.1:5075")

import h5py  # noqa: E402
from epics import caget, caput  # noqa: E402

PANDA = "XF:27ID1-ES{PANDA:1}:"
MOTOR = "XF:27IDF-OP:1{MC:5-Ax:4}Mtr"


def wait_for(fn, timeout, why):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(0.2)
    print("TIMEOUT waiting for", why)
    return False


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", default="/tmp/hex-sim-data")
    p.add_argument("--file-name", default="panda.hdf")
    p.add_argument("--dataset", default="Angle")
    p.add_argument("--start-deg", type=float, default=0.0)
    p.add_argument("--velo", type=float, default=2.0)
    p.add_argument("--pulses", type=int, default=5)
    p.add_argument("--step-s", type=float, default=0.4)
    p.add_argument("--width-s", type=float, default=0.05)
    args = p.parse_args(argv)

    path = os.path.join(args.data_dir, args.file_name)
    if os.path.exists(path):
        os.remove(path)

    # Park the motor, let the bridge settle, read the compare-domain baseline.
    caput(MOTOR + ".VELO", 10.0, wait=True)
    caput(MOTOR + ".VAL", args.start_deg, wait=True)
    time.sleep(1.5)
    calc1 = int(caget(PANDA + "CALC1:OUT"))
    start = calc1 + 200
    move_s = args.pulses * args.step_s + 2.0
    target_deg = args.start_deg + args.velo * move_s
    print("CALC1 baseline %d; PCOMP START=%d; move %.1f -> %.1f deg"
          % (calc1, start, args.start_deg, target_deg))

    # Scan parameters over the IOC, deco.Panda-style (time mode).
    caput(PANDA + "PCOMP1:START", start, wait=True)
    caput(PANDA + "PCOMP1:PULSES", 1, wait=True)
    caput(PANDA + "PULSE1:PULSES", args.pulses, wait=True)
    caput(PANDA + "PULSE1:STEP", args.step_s, wait=True)
    caput(PANDA + "PULSE1:WIDTH", args.width_s, wait=True)
    caput(PANDA + "PULSE1:DELAY", 0.0, wait=True)

    # Dataset name + HDF writer config, then start writer and arm.
    caput(PANDA + "CALC2:OUT:DATASET", args.dataset, wait=True)
    caput(PANDA + "DATA:HDFDirectory", args.data_dir, wait=True)
    caput(PANDA + "DATA:HDFFileName", args.file_name, wait=True)
    caput(PANDA + "DATA:NumCapture", args.pulses, wait=True)
    caput(PANDA + "DATA:Capture", 1, wait=True)
    time.sleep(0.5)
    caput(PANDA + "PCAP:ARM", 1, wait=True)
    ok = wait_for(lambda: caget(PANDA + "PCAP:ACTIVE") == 1, 5, "PCAP:ACTIVE")

    try:
        caput(MOTOR + ".VELO", args.velo, wait=True)
        caput(MOTOR + ".VAL", target_deg, wait=False)
        ok = wait_for(
            lambda: caget(PANDA + "DATA:NumCaptured") >= args.pulses,
            60, "DATA:NumCaptured >= %d" % args.pulses) and ok
        n = int(caget(PANDA + "DATA:NumCaptured"))
        print("DATA:NumCaptured =", n)
    finally:
        caput(PANDA + "PCAP:ARM", 0, wait=True)
        time.sleep(0.5)
        caput(PANDA + "DATA:Capture", 0, wait=True)
        caput(MOTOR + ".VELO", 10.0, wait=True)
        time.sleep(1.0)

    if not os.path.exists(path):
        print("FAIL: no HDF file at", path)
        return 1
    with h5py.File(path, "r") as f:
        keys = list(f.keys())
        print("HDF datasets:", keys)
        if args.dataset not in f:
            print("FAIL: dataset %r not in %s" % (args.dataset, path))
            return 1
        data = f[args.dataset][()]
    vals = [round(float(v), 3) for v in data.ravel().tolist()]
    print("%s (degrees) = %s" % (args.dataset, vals))
    in_degrees = all(-360.0 < v < 360.0 for v in vals)
    if ok and n >= args.pulses and len(vals) == args.pulses and in_degrees:
        print("PASS")
        return 0
    print("FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
