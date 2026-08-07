#!/usr/bin/env python3
"""TTL trigger bridge test: every PandA pulse takes exactly one camera frame.

The full simulated wire, end to end: a REAL motor move -> motor->INENC bridge
-> the recovered ``Tomo_radio_1_config`` chain (PCOMP fires once, PULSE1 emits
a time train, COUNTER3 tallies it) -> the TTL trigger bridge poking one
``cam1:Acquire`` per pulse on the REAL Kinetix AreaDetector IOC
(``ImageMode=Single``) -> ``cam1:ArrayCounter_RBV`` advances by exactly the
pulse count.

Prereqs (PROGRESS.md bring-up): panda sim + real tomo design + motor IOC
(:5075) + the motor bridge + the Kinetix AD IOC (:5085), all running. The
TTL bridge itself is started BY this test.

Run (env with `pyepics`): python tests/ttl_bridge_test.py
Exit code 0 = PASS.
"""

import argparse
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from localguard import assert_local_epics  # noqa: E402

assert_local_epics(
    default_ca="127.0.0.1:5075 127.0.0.1:5085 127.0.0.1:5095")

from epics import caget, caput  # noqa: E402

MOTOR = "XF:27IDF-OP:1{MC:5-Ax:4}Mtr"
CAM = "XF:27ID1-BI{Kinetix-Det:1}cam1:"
COUNTER = "XF:27ID1-ES{PANDA:1}:COUNTER3:OUT"
HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(HERE, "..", "ttl_trigger_bridge.py")


def cmd(sock, line):
    sock.sendall((line + "\n").encode())
    time.sleep(0.03)
    return sock.recv(4096).decode().strip()


def read_int(sock, field):
    return int(cmd(sock, field + "?").split("=", 1)[1])


def wait_move_done(target_deg, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.2)
        if caget(MOTOR + ".DMOV") == 1 and \
                abs(caget(MOTOR + ".RBV") - target_deg) < 0.05:
            return True
    return False


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--ctrl-port", type=int, default=8888)
    p.add_argument("--start-deg", type=float, default=0.0)
    p.add_argument("--velo", type=float, default=2.0)
    p.add_argument("--pulses", type=int, default=5)
    p.add_argument("--step-s", type=float, default=0.4)
    p.add_argument("--width-s", type=float, default=0.05)
    args = p.parse_args(argv)

    # The tally PV must exist (pandablocks-ioc publishes position-bus OUTs).
    if caget(COUNTER, timeout=5) is None:
        print("FAIL: %s not reachable — is the panda IOC up?" % COUNTER)
        return 1

    # Camera: one frame per Acquire poke; remember state to restore.
    prior_mode = caget(CAM + "ImageMode")
    prior_time = caget(CAM + "AcquireTime")
    caput(CAM + "ImageMode", 0, wait=True)          # Single
    caput(CAM + "AcquireTime", 0.05, wait=True)

    ctrl = socket.create_connection((args.host, args.ctrl_port))
    ctrl.settimeout(2)
    bridge = None
    try:
        # Park the motor, let the INENC bridge settle, take baselines.
        caput(MOTOR + ".VELO", 10.0, wait=True)
        caput(MOTOR + ".VAL", args.start_deg, wait=True)
        wait_move_done(args.start_deg)
        time.sleep(1.5)
        calc1 = read_int(ctrl, "CALC1.OUT")
        start = calc1 + 200  # 1 deg past park in the negated domain
        move_s = args.pulses * args.step_s + 2.0
        target_deg = args.start_deg + args.velo * move_s
        for c in [
            "PCOMP1.START=%d" % start,
            "PCOMP1.STEP=200",
            "PCOMP1.WIDTH=3",
            "PCOMP1.PULSES=1",
            "PULSE1.PULSES=%d" % args.pulses,
            "PULSE1.STEP=%g" % args.step_s,
            "PULSE1.WIDTH=%g" % args.width_s,
            "PULSE1.DELAY=0",
        ]:
            cmd(ctrl, c)

        # Start the TTL bridge AFTER parking, so its baseline is clean.
        bridge = subprocess.Popen(
            [sys.executable, "-u", BRIDGE],
            env=dict(os.environ,
                     EPICS_CA_ADDR_LIST="127.0.0.1:5085 127.0.0.1:5095",
                     EPICS_CA_AUTO_ADDR_LIST="NO"),
        )
        time.sleep(3)
        frames0 = int(caget(CAM + "ArrayCounter_RBV"))

        print("ARM ->", cmd(ctrl, "*PCAP.ARM="),
              "ACTIVE ->", cmd(ctrl, "PCAP.ACTIVE?"))
        # COUNTER3.ENABLE follows PCAP.ACTIVE, so ARM resets the tally —
        # baseline it AFTER arming (the bridge rebaselines on the drop too).
        time.sleep(0.3)
        tally0 = int(caget(COUNTER))
        print("baselines: tally=%d frames=%d; PCOMP START=%d; move -> %.1f deg"
              % (tally0, frames0, start, target_deg))
        caput(MOTOR + ".VELO", args.velo, wait=True)
        caput(MOTOR + ".VAL", target_deg, wait=False)
        if not wait_move_done(target_deg):
            print("WARN: move did not finish in time")
        time.sleep(2.5)  # bridge catch-up + last exposure
    finally:
        cmd(ctrl, "*PCAP.DISARM=")
        time.sleep(0.5)
        caput(MOTOR + ".VELO", 10.0, wait=True)
        ctrl.close()
        if bridge is not None:
            bridge.terminate()
            bridge.wait(timeout=5)
        if prior_mode is not None:
            caput(CAM + "ImageMode", prior_mode, wait=True)
        if prior_time is not None:
            caput(CAM + "AcquireTime", prior_time, wait=True)

    pulses = int(caget(COUNTER)) - tally0
    frames = int(caget(CAM + "ArrayCounter_RBV")) - frames0
    print("pulses tallied: %d, frames taken: %d (expected %d each)"
          % (pulses, frames, args.pulses))
    if pulses == args.pulses and frames == pulses:
        print("PASS")
        return 0
    print("FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
