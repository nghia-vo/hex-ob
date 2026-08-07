#!/usr/bin/env python3
"""Bridge the simulated rotary motor position into the simulated PandA encoder.

On the real HEX beamline the rotation stage's physical encoder is wired into the
PandABlocks ``INENC`` input, so as the stage sweeps, PandA sees the position and
``PCOMP`` fires position-compare pulses that trigger ``PCAP`` capture. In the sim
the motor is a separate caproto ``FakeMotor`` IOC with no electrical link to the
PandA sim, so PandA never sees the motion and nothing is captured.

This bridge closes that loop in software: it monitors the motor readback (``RBV``)
over Channel Access and feeds the equivalent encoder counts into the sim PandA's
``INENC1.VAL`` through the sim server's position-injection channel (default
127.0.0.1:9101, opened by ``fpga_sim_server.py``). With the HEX tomo design
loaded (``hex_tomo_design.py``), sweeping the motor then drives
PCOMP -> PULSE -> PCAP capture exactly as the hardware would.

Counts convention (matches the real ``Tomo_radio_1_config`` design — see
``designs/tomo_radio_1_config.md``): the RAW encoder runs backwards,
``INENC1.VAL = angle_deg * COUNTS_PER_DEG + OFFSET`` with defaults **-200 and
+39660**; the design's CALC1 (TYPEA=-Value) negates it so the pyepics
convention ``CALC-domain = 200*deg - 39660`` (``get_enc_value``) holds.

The injected position is SLEW-LIMITED toward the latest readback: a real
encoder is continuous, but ``FakeMotor`` publishes ``RBV`` in coarse ~10 Hz
steps, and one big step can hop the PandA ``PCOMP`` past several compare
points at once (0 captures). Each tick (``--rate-hz``) the bridge moves at
most ``--max-step`` counts toward the target, so the sim PandA always sees a
smooth monotonic ramp. Effective slew ceiling = ``max_step * rate_hz``
counts/s (defaults 50 x 50 = 2500 counts/s = 12.5 deg/s at 200 counts/deg);
raise either flag for faster scans.

Only the LOCAL sim is written; the motor is only read. Never point this at a
real beamline.
"""

import argparse
import os
import socket
import sys
import threading
import time

DEFAULT_MOTOR_PV = "XF:27IDF-OP:1{MC:5-Ax:4}Mtr.RBV"


class Injector:
    """Reconnecting line client for the sim server injection channel."""

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sock = None
        self._lock = threading.Lock()

    def _connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=5)
        self.sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)

    def send(self, block, field, value):
        line = "%s %s %d\n" % (block, field, int(round(value)))
        with self._lock:
            for _attempt in (1, 2):
                try:
                    if self.sock is None:
                        self._connect()
                    self.sock.sendall(line.encode())
                    self._drain_replies()
                    return True
                except OSError:
                    try:
                        if self.sock:
                            self.sock.close()
                    finally:
                        self.sock = None
        return False

    def _drain_replies(self):
        # The server acknowledges each line; discard the acks so neither
        # side's socket buffer ever fills (an unread reply stream is what
        # wedged the injection channel before the server went non-blocking).
        self.sock.setblocking(False)
        try:
            while True:
                if not self.sock.recv(4096):
                    raise OSError("injection channel closed")
        except (BlockingIOError, InterruptedError):
            pass
        finally:
            self.sock.setblocking(True)
            self.sock.settimeout(5)


def run(args):
    # Beamline guard BEFORE importing epics (it reads the env at import):
    # refuse to run against anything but loopback — this process WRITES what
    # real-PV-named motors report, so a beamline network must be unreachable.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from localguard import assert_local_epics
    assert_local_epics(default_ca="127.0.0.1:5075")

    from epics import PV  # imported here so --help works without pyepics

    inj = Injector(args.inject_host, args.inject_port)

    def counts(angle_deg):
        return angle_deg * args.counts_per_deg + args.offset

    state = {"target": None}

    def on_value(value=None, **_kw):
        if value is not None:
            state["target"] = counts(float(value))

    pv = PV(args.motor_pv, callback=on_value, auto_monitor=True)
    if not pv.wait_for_connection(timeout=args.connect_timeout):
        print("ERROR: could not connect to %s" % args.motor_pv, file=sys.stderr)
        return 1
    # Seed the initial position immediately.
    initial = pv.get()
    if initial is not None:
        on_value(value=initial)
    print(
        "Bridging %s -> %s:%d [%s.%s]  (counts = deg*%g + %g, "
        "slew <= %g counts/tick @ %g Hz)"
        % (
            args.motor_pv,
            args.inject_host,
            args.inject_port,
            args.block,
            args.field,
            args.counts_per_deg,
            args.offset,
            args.max_step,
            args.rate_hz,
        )
    )

    # camonitor updates the target; this loop ramps the injected position
    # toward it, at most max_step counts per tick, so PCOMP sees every compare
    # point crossed in its own sample instead of one coarse RBV-sized jump.
    sent = None
    try:
        while True:
            time.sleep(1.0 / max(args.rate_hz, 1e-3))
            target = state["target"]
            if target is None:
                continue
            target = int(round(target))
            if sent is None or abs(target - sent) <= args.max_step:
                nxt = target
            elif target > sent:
                nxt = sent + args.max_step
            else:
                nxt = sent - args.max_step
            if nxt != sent:
                if inj.send(args.block, args.field, nxt):
                    sent = nxt
    except KeyboardInterrupt:
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--motor-pv", default=DEFAULT_MOTOR_PV)
    parser.add_argument("--inject-host", default="127.0.0.1")
    parser.add_argument("--inject-port", type=int, default=9101)
    parser.add_argument("--block", default="INENC1")
    parser.add_argument("--field", default="VAL")
    parser.add_argument("--counts-per-deg", type=float, default=-200.0)
    parser.add_argument("--offset", type=float, default=39660.0)
    # Slew ceiling = rate_hz * max_step counts/s. Defaults 100 Hz x 150 =
    # 15000 counts/s = 75 deg/s at 200 counts/deg — above tomo_flyscan.py's
    # max_velo of 60 deg/s, so the simulated encoder never lags a scan (an
    # early 50x50 default halved a 25 deg/s scan's captured angle range).
    parser.add_argument("--rate-hz", type=float, default=100.0)
    parser.add_argument(
        "--max-step", type=int, default=150,
        help="max injected counts per tick (slew limit; with --rate-hz sets "
             "the deg/s ceiling the simulated encoder can follow)")
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
