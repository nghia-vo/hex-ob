#!/usr/bin/env python3
"""PandA sim capture test: a REAL motor move through the REAL design.

End-to-end over every live piece: caproto FakeMotor IOC (:5075) -> the
motor->INENC bridge (raw = 39660 - 200*deg) -> injection channel -> the
recovered ``Tomo_radio_1_config`` chain: arm PCAP (arming IS the gate),
CALC1 (negate) rises through PCOMP1.START as the stage rotates, PCOMP fires
ONCE, PULSE1 emits a time train of PULSES pulses, one captured row each.

No BITS.A involved -- that is the radiography path in the real design.

Prereqs (PROGRESS.md bring-up): panda sim + real tomo design + motor IOC
(:5075) + the bridge, all running.

Run (env with `pandablocks` + `pyepics`):
    python tests/slowmove_capture_test.py
Exit code 0 = PASS.
"""

import argparse
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from localguard import assert_local_epics  # noqa: E402

assert_local_epics(default_ca="127.0.0.1:5075")

from epics import caget, caput  # noqa: E402
from pandablocks.blocking import BlockingClient  # noqa: E402
from pandablocks.responses import EndData, FrameData  # noqa: E402

MOTOR = "XF:27IDF-OP:1{MC:5-Ax:4}Mtr"


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
    p.add_argument("--step-s", type=float, default=0.4,
                   help="PULSE1 train period (seconds)")
    p.add_argument("--width-s", type=float, default=0.05)
    args = p.parse_args(argv)

    ctrl = socket.create_connection((args.host, args.ctrl_port))
    ctrl.settimeout(2)

    # Park the motor, let the bridge settle, read the compare-domain baseline.
    caput(MOTOR + ".VELO", 10.0, wait=True)
    caput(MOTOR + ".VAL", args.start_deg, wait=True)
    wait_move_done(args.start_deg)
    time.sleep(1.5)
    calc1 = read_int(ctrl, "CALC1.OUT")
    start = calc1 + 200  # 1 deg past park in the negated domain
    # Move far enough that the whole time train happens during motion.
    move_s = args.pulses * args.step_s + 2.0
    target_deg = args.start_deg + args.velo * move_s
    print("CALC1 baseline %d; PCOMP START=%d (fires once); move %.1f -> %.1f deg"
          % (calc1, start, args.start_deg, target_deg))

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

    frames = []

    def collect():
        cl = BlockingClient(args.host)
        cl.connect()
        try:
            for d in cl.data(scaled=False):
                if isinstance(d, FrameData):
                    frames.extend(d.data.tolist())
                elif isinstance(d, EndData):
                    break
        except Exception as e:  # noqa: BLE001 - report and fall through
            print("data stream:", type(e).__name__, e)
        finally:
            cl.close()

    threading.Thread(target=collect, daemon=True).start()
    time.sleep(0.3)

    try:
        print("ARM ->", cmd(ctrl, "*PCAP.ARM="),
              "ACTIVE ->", cmd(ctrl, "PCAP.ACTIVE?"))
        caput(MOTOR + ".VELO", args.velo, wait=True)
        caput(MOTOR + ".VAL", target_deg, wait=False)
        if not wait_move_done(target_deg):
            print("WARN: move did not finish in time")
        time.sleep(1.5)  # let the slew-limited bridge catch up
    finally:
        cmd(ctrl, "*PCAP.DISARM=")
        time.sleep(0.5)
        caput(MOTOR + ".VELO", 10.0, wait=True)
        ctrl.close()

    print("RBV final: %.3f" % caget(MOTOR + ".RBV"))
    print("captured %d row(s): %s" % (len(frames), frames))
    if len(frames) == args.pulses:
        print("PASS")
        return 0
    print("FAIL: expected %d rows" % args.pulses)
    return 1


if __name__ == "__main__":
    sys.exit(main())
