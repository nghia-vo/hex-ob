#!/usr/bin/env python3
"""Bridge the simulated PandA's trigger pulses to the camera, per pulse.

On the real HEX beamline the tomo design's PULSE1 train leaves the PandA on
``TTLOUT1`` and a TTL cable carries it to the Kinetix trigger input, so every
position-armed pulse exposes exactly one frame. In the sim the pulse train is
fully real inside the FPGA block-sim engine — PCOMP fires, PULSE1 emits the
train, PCAP captures the angle per pulse — but there is no cable, so the
frame-tier camera (a real AreaDetector IOC) free-runs instead of following it.

This bridge closes that last inch in software, the sibling of
``motor_encoder_bridge.py``: it monitors the **cumulative pulse tally** over
Channel Access — ``COUNTER3:OUT``, which the design wires to ``PULSE1.OUT``
(the same tally that matches the real box's 1801) — and issues one camera
``Acquire`` per new pulse. Watching the cumulative counter rather than the
pulse bit means a missed monitor update heals itself: the next update carries
the total, and the bridge catches up by the delta.

USE IS OPT-IN. Run it only when the camera is configured one-frame-per-poke
(``cam1:ImageMode=Single``); the pyepics oracle (``tomo_flyscan.py``)
free-runs the camera and must NOT have this bridge running — it would
double-drive ``Acquire``. Like the real camera, back-to-back pulses inside an
exposure are lost to deadtime: a poke while ``Acquire`` is still busy takes no
extra frame.

Only the LOCAL sim is written; the PandA is only read. Never point this at a
real beamline.
"""

import argparse
import os
import sys
import time

DEFAULT_COUNTER_PV = "XF:27ID1-ES{PANDA:1}:COUNTER3:OUT"
DEFAULT_ACQUIRE_PV = "XF:27ID1-BI{Kinetix-Det:1}cam1:Acquire"


def run(args):
    # Beamline guard BEFORE importing epics (it reads the env at import):
    # this process WRITES a real-PV-named camera's Acquire, so a beamline
    # network must be unreachable by construction.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from localguard import assert_local_epics
    assert_local_epics(default_ca="127.0.0.1:5085 127.0.0.1:5095")

    from epics import PV  # imported here so --help works without pyepics

    state = {"tally": None}

    def on_value(value=None, **_kw):
        if value is not None:
            state["tally"] = int(value)

    counter = PV(args.counter_pv, callback=on_value, auto_monitor=True)
    acquire = PV(args.acquire_pv)
    for pv in (counter, acquire):
        if not pv.wait_for_connection(timeout=args.connect_timeout):
            print("ERROR: could not connect to %s" % pv.pvname, file=sys.stderr)
            return 1

    # Baseline = the tally as found; pulses from before the bridge started
    # (or from a previous scan) must not fire the camera retroactively.
    initial = counter.get()
    if initial is not None:
        on_value(value=initial)
    handled = state["tally"]
    print("Bridging %s -> %s  (baseline tally %s, catchup <= %d/tick @ %g Hz)"
          % (args.counter_pv, args.acquire_pv, handled, args.max_catchup,
             args.rate_hz))

    try:
        while True:
            time.sleep(1.0 / max(args.rate_hz, 1e-3))
            tally = state["tally"]
            if tally is None:
                continue
            if handled is None or tally < handled:
                # First reading, or the counter was reset (disarm/re-enable):
                # rebaseline without firing.
                handled = tally
                continue
            # Catch up by the delta, bounded so a stale bridge restarted
            # against a huge tally can't machine-gun the camera.
            for _ in range(min(tally - handled, args.max_catchup)):
                acquire.put(1, wait=args.wait_each,
                            timeout=args.acquire_timeout)
            if tally != handled:
                print("pulses %d -> %d: poked Acquire x%d"
                      % (handled, tally, min(tally - handled,
                                             args.max_catchup)))
            handled = tally
    except KeyboardInterrupt:
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counter-pv", default=DEFAULT_COUNTER_PV,
                        help="cumulative pulse tally (COUNTER3.TRIG=PULSE1.OUT)")
    parser.add_argument("--acquire-pv", default=DEFAULT_ACQUIRE_PV)
    parser.add_argument("--rate-hz", type=float, default=50.0)
    parser.add_argument("--max-catchup", type=int, default=20,
                        help="max Acquire pokes per tick when catching up")
    parser.add_argument("--wait-each", action="store_true",
                        help="wait for each exposure to complete before the "
                             "next poke (serializes catch-up bursts)")
    parser.add_argument("--acquire-timeout", type=float, default=5.0)
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
