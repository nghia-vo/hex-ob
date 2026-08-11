#!/usr/bin/env python3
"""Make armed-external WAIT in the sim: hold the camera until the train fires.

On a real Kinetix, arming with an external trigger mode means WAITING: no
frames until TTL pulses arrive. The sim's frame tier is an ADSimDetector,
which free-runs the moment it is armed — so an ophyd-async fly scan (which
arms during ``prepare``) leaks frames into the HDF capture before
``kickoff`` and fails the kickoff range check ("Kickoff requested N:M...").

Mechanism (two guards, both needed):

1. **Hold the camera.** When ``Acquire`` goes 1 while TriggerMode is an
   external state and the pulse train has not started, the bridge
   immediately stops it again (monitor-callback latency ~ms, well inside
   one exposure). When the cumulative pulse tally (``COUNTER3:OUT``, wired
   to PULSE1.OUT) increments — the train is really firing — the bridge
   restarts ``Acquire``: the camera then runs its FULL armed ``NumImages``
   fresh, so the frame count is exact by construction.
2. **Gate the plugin chain.** From the moment TriggerMode goes external,
   ``Trans1:EnableCallbacks`` (the source the HDF/PVA plugins consume from
   in the reset default routing) is disabled until the restart — so even a
   frame that completes in the stop race never reaches the writer.

USE IS OPT-IN, like ttl_trigger_bridge: run it for ophyd-async fly-scan
work against the sim. The pyepics oracle must NOT have it running — the
oracle also sets an external trigger mode, and its frozen free-run
behavior is the D-0014 reference.

Only the LOCAL sim is written; never point this at a real beamline.
"""

import argparse
import fcntl
import os
import signal
import sys
import time

DEFAULT_COUNTER_PV = "XF:27ID1-ES{PANDA:1}:COUNTER3:OUT"
DEFAULT_CAM_PREFIX = "XF:27ID1-BI{Kinetix-Det:1}cam1:"
DEFAULT_GATE_PV = "XF:27ID1-BI{Kinetix-Det:1}Trans1:EnableCallbacks"
# TriggerMode states counted as "external" (init_kinetix's extended mbbo).
EXTERNAL_STATES = ("Rising Edge", "Exp. Gate", "External")


def run(args):
    # Beamline guard BEFORE importing epics (it reads the env at import):
    # this process WRITES a real-PV-named camera's Acquire.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from localguard import assert_local_epics
    assert_local_epics(default_ca="127.0.0.1:5085 127.0.0.1:5095")

    # Single instance only: two bridges fight over Acquire (both hold and
    # both restart), doubling hold/release cycles and leaking frames. The
    # flock dies with the process — any exit, even SIGKILL, releases it.
    lock = open("/tmp/hex-armed-gate-bridge.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("ERROR: another armed_gate_bridge is already running "
              "(holds /tmp/hex-armed-gate-bridge.lock) — kill it first.",
              file=sys.stderr)
        return 1

    from epics import PV  # imported here so --help works without pyepics

    state = {
        "tally": None,
        "mode": None,
        "held": False,        # we stopped an armed camera; restart on train
        "tally_at_hold": None,
        "restarting": False,  # our own Acquire=1 put; don't re-hold it
    }

    def external() -> bool:
        return state["mode"] in EXTERNAL_STATES

    def on_tally(value=None, **_kw):
        if value is not None:
            state["tally"] = int(value)

    def on_mode(char_value=None, **_kw):
        if char_value:
            state["mode"] = char_value

    def on_acquire(value=None, **_kw):
        # Callback-driven hold: beat the first exposure (~ms latency).
        if value == 1 and external() and not state["restarting"]:
            acquire.put(0, wait=False)
            state["held"] = True
            state["tally_at_hold"] = state["tally"]
            print("HOLD: armed-external — camera stopped "
                  f"(tally={state['tally_at_hold']})", flush=True)

    counter = PV(args.counter_pv, callback=on_tally, auto_monitor=True)
    mode = PV(args.cam_prefix + "TriggerMode_RBV", callback=on_mode,
              auto_monitor=True)
    acquire = PV(args.cam_prefix + "Acquire", callback=on_acquire,
                 auto_monitor=True)
    gate = PV(args.gate_pv)
    for pv in (counter, mode, acquire, gate):
        if not pv.wait_for_connection(timeout=args.connect_timeout):
            print("ERROR: could not connect to %s" % pv.pvname, file=sys.stderr)
            return 1

    on_tally(value=counter.get())
    on_mode(char_value=mode.get(as_string=True))

    gated = False
    released = False   # train fired; stay open until the mode goes internal
    print(f"armed_gate_bridge up: watching {args.cam_prefix} + {args.counter_pv}, "
          f"gating {args.gate_pv}", flush=True)

    # The sim test harness stops the bridge with SIGTERM (Popen.terminate),
    # not Ctrl-C — route it through the same cleanup path.
    def on_sigterm(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_sigterm)

    try:
        while True:
            time.sleep(1.0 / max(args.rate_hz, 1e-3))

            if not external():
                # Back to internal: normal live-view routing, fresh cycle.
                released = False
                state["held"] = False
                if gated:
                    gate.put(1, wait=True, timeout=args.put_timeout)
                    gated = False
                    print("gate OPEN (internal trigger mode)", flush=True)
                continue

            # External mode: closed while waiting for the train, open once
            # released (the running acquisition's frames must flow).
            if not released and not gated:
                gate.put(0, wait=True, timeout=args.put_timeout)
                gated = True
                print("gate CLOSED (external trigger mode, awaiting train)",
                      flush=True)

            if state["held"]:
                tally = state["tally"]
                t0 = state["tally_at_hold"]
                if tally is not None and t0 is not None and tally < t0:
                    # Arming resets COUNTER3 to 0 — rebaseline (same case
                    # ttl_trigger_bridge handles) so the first real pulse
                    # is seen as an increment.
                    state["tally_at_hold"] = t0 = tally
                if tally is not None and t0 is not None and tally > t0:
                    # The train is firing: open the chain and run the full
                    # armed acquisition.
                    released = True
                    if gated:
                        gate.put(1, wait=True, timeout=args.put_timeout)
                        gated = False
                    state["restarting"] = True
                    acquire.put(1, wait=False)
                    state["held"] = False
                    print(f"RELEASE: train started (tally {t0} -> {tally}) — "
                          "camera restarted, gate open", flush=True)
                    # Small grace so our own Acquire monitor echo passes.
                    time.sleep(0.2)
                    state["restarting"] = False
    except KeyboardInterrupt:
        return 0
    finally:
        gate.put(1, wait=False)  # never leave the chain gated, on ANY exit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counter-pv", default=DEFAULT_COUNTER_PV,
                        help="cumulative pulse tally (COUNTER3.TRIG=PULSE1.OUT)")
    parser.add_argument("--cam-prefix", default=DEFAULT_CAM_PREFIX)
    parser.add_argument("--gate-pv", default=DEFAULT_GATE_PV,
                        help="upstream plugin EnableCallbacks PV to gate")
    parser.add_argument("--rate-hz", type=float, default=100.0)
    parser.add_argument("--put-timeout", type=float, default=5.0)
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
