#!/usr/bin/env python3
"""PandA sim capture test: injected position sweep through the REAL design.

Exercises the recovered ``Tomo_radio_1_config`` chain WITHOUT motor or bridge:
arm PCAP (arming IS the gate: PCOMP1.ENABLE = PCAP.ACTIVE), sweep the RAW
encoder DOWNWARD via the injection channel (:9101) — the design's CALC1
negates it, so the position PCOMP1 watches rises through START — PCOMP fires
ONCE and PULSE1 emits a time train of PULSES pulses, each capturing one row.

Prereqs: hexsim-panda-sim running with the real design applied
(``python iocs/panda/hex_tomo_design.py``). STOP the motor->INENC bridge
first -- it injects positions too and would fight the sweep.

Run (env with `pandablocks`): python tests/inj_capture_test.py
Exit code 0 = PASS.
"""

import argparse
import socket
import sys
import threading
import time

from pandablocks.blocking import BlockingClient
from pandablocks.responses import EndData, FrameData


def cmd(sock, line):
    sock.sendall((line + "\n").encode())
    time.sleep(0.03)
    return sock.recv(4096).decode().strip()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--ctrl-port", type=int, default=8888)
    p.add_argument("--inject-port", type=int, default=9101)
    p.add_argument("--pulses", type=int, default=5)
    p.add_argument("--step-s", type=float, default=0.3,
                   help="PULSE1 train period (seconds)")
    p.add_argument("--width-s", type=float, default=0.05)
    args = p.parse_args(argv)

    ctrl = socket.create_connection((args.host, args.ctrl_port))
    ctrl.settimeout(2)
    inj = socket.create_connection((args.host, args.inject_port))
    inj.settimeout(2)

    def put_raw(v):
        inj.sendall(("INENC1 VAL %d\n" % v).encode())
        try:
            inj.recv(100)
        except socket.timeout:
            pass

    # Park the raw encoder at "0 deg" (raw = 39660), let CALC1 settle.
    raw0 = 39660
    put_raw(raw0)
    time.sleep(0.3)
    calc1 = int(cmd(ctrl, "CALC1.OUT?").split("=", 1)[1])
    start = calc1 + 200  # one degree past park, in the negated (CALC1) domain
    print("CALC1.OUT baseline %d; PCOMP window START=%d (single compare point)"
          % (calc1, start))

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
        print(c, "->", cmd(ctrl, c))

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
    time.sleep(0.5)

    try:
        print("ARM ->", cmd(ctrl, "*PCAP.ARM="))
        time.sleep(0.3)
        print("ACTIVE ->", cmd(ctrl, "PCAP.ACTIVE?"),
              "| PCOMP1 enabled by arm:", cmd(ctrl, "PCOMP1.ACTIVE?"))
        # Sweep raw encoder DOWN (angle up) across the compare point, then
        # keep moving during the whole pulse train so samples differ.
        train_s = args.pulses * args.step_s + 0.5
        t0 = time.time()
        while time.time() - t0 < train_s + 1.0:
            angle = 2.0 * (time.time() - t0)  # 2 deg/s sweep
            put_raw(raw0 - int(200 * angle))
            time.sleep(0.02)
    finally:
        print("DISARM ->", cmd(ctrl, "*PCAP.DISARM="))
        time.sleep(0.5)
        ctrl.close()
        inj.close()

    print("captured %d row(s): %s" % (len(frames), frames))
    if len(frames) == args.pulses:
        print("PASS")
        return 0
    print("FAIL: expected %d rows" % args.pulses)
    return 1


if __name__ == "__main__":
    sys.exit(main())
