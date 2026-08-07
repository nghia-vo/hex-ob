"""Dedicated simulated motor IOC for the HEX sim (own process, own CA port).

Runs caproto ``FakeMotor`` record(s) in a **dedicated** event loop so the ~10 Hz
motion simulator is never starved by the detector + fabrication load in the main
``sim_ioc.py`` server. This is the configuration that works reliably: a motor
IOC on its own, exactly like caproto's shipped ``fake_motor_record`` example.

CA coexistence with ``sim_ioc`` (blackhole + Kinetix on :5064):
  * serve this IOC on a separate CA server port (default 5075),
  * add ``127.0.0.1:5075`` to the client ``EPICS_CA_ADDR_LIST`` (see env.sh), and
  * have ``sim_ioc``'s blackhole EXCLUDE the motor namespace so only this IOC
    answers the motor searches (no conflict).

The simulator is patched (``patch_fake_motor``) so zero/tiny-distance moves still
produce an observable ``.DMOV`` 1->0->1 cycle (ophyd v1 ``MoveStatus`` needs it).

Run (in an env with caproto — hex-profile-collection ``terminal``):
    EPICS_CAS_SERVER_PORT=5075 python iocs/motor/motor_ioc.py [--motor-pvs ...]
or pass ``--port 5075`` and this script sets the env var for you.
"""

import argparse
import os
import sys
from pathlib import Path

# sim_devices/ is a sibling package dir; make motor_sim importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sim_devices"))

from motor_sim import (  # noqa: E402
    DEFAULT_MOTOR_PVS,
    motor_group,
    patch_fake_motor,
)


def main():
    ap = argparse.ArgumentParser(description="Dedicated HEX sim motor IOC.")
    ap.add_argument(
        "--motor-pvs",
        nargs="*",
        default=DEFAULT_MOTOR_PVS,
        help="Motor-record PVs to serve (default: tomography rotation MC:5-Ax:4).",
    )
    ap.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("EPICS_CAS_SERVER_PORT", 5075)),
        help="CA server port for this IOC (default 5075).",
    )
    args = ap.parse_args()

    # Set the server port BEFORE importing caproto.server so it binds correctly.
    os.environ["EPICS_CAS_SERVER_PORT"] = str(args.port)
    os.environ["EPICS_CA_SERVER_PORT"] = str(args.port)
    from caproto.server import run  # noqa: E402  (after port env set)

    patch_fake_motor()  # zero-move-safe simulator

    pvdb = {}
    groups = []
    for pv in args.motor_pvs:
        group = motor_group(pv)
        groups.append(group)  # keep refs alive (startup coroutine bound to them)
        pvdb.update(group.pvdb)

    print(
        f"[hex-sim-motor] serving {len(groups)} motor record(s) {list(args.motor_pvs)} "
        f"on 127.0.0.1:{args.port} (Ctrl-C to stop)"
    )
    run(pvdb, interfaces=["127.0.0.1"])


if __name__ == "__main__":
    main()
