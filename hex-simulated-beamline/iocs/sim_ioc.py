"""Unified HEX simulation IOC (single CA server).

Serves each configured per-detector device with its **exact typed PV set**
(introspected from the ophyd-async device) and **fabricates everything else**
(motors, shutters, plugins) via the vendored blackhole. Running one caproto
server avoids the CA search-port (UDP 5064) conflict that makes multiple
caproto servers on one host deaf to each other.

The per-detector PV sets come from the same modules used standalone
(`kinetix_sim.build_kinetix` + `_ophyd_async_sim.build_pvdb`), so the
"per-detector sim" definitions stay clean and reusable — they're just composed
into one server here.

Run in an env with ophyd-async + caproto (hex-profile-collection `terminal`):
    python iocs/sim_ioc.py [--kinetix-ids 1 3]
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from caproto.server import run

# sim_devices/ is a sibling package dir; make its modules importable.
sys.path.insert(0, str(Path(__file__).with_name("sim_devices")))
sys.path.insert(0, str(Path(__file__).with_name("blackhole")))

from _ophyd_async_sim import build_pvdb  # noqa: E402
from kinetix_sim import build_kinetix, build_kinetix_overlay_pvdb  # noqa: E402
from motor_sim import DEFAULT_MOTOR_PVS, record_exclude_prefix  # noqa: E402

# NOTE: spoof_beamline (blackhole) is imported *inside* main(), AFTER we set
# BLACKHOLE_EXCLUDE_PREFIXES, because blackhole reads that env var at import.
# The motors themselves are served by the dedicated motor IOC (iocs/motor/
# motor_ioc.py) on its own CA port; here we only tell blackhole to stay silent
# for the motor namespace so there's no CA search conflict.


def build_detector_pvdb(kinetix_ids):
    devices = [
        build_kinetix(f"XF:27ID1-BI{{Kinetix-Det:{i}}}") for i in kinetix_ids
    ]

    async def _connect_all():
        for dev in devices:
            await dev.connect(mock=True)

    asyncio.run(_connect_all())
    typed: dict = {}
    for dev in devices:
        typed.update(build_pvdb(dev))
    # Detector-sim pre-flight defaults: the ophyd-async HDF writer refuses to
    # open unless the plugin reports the file path exists. The sim writes nowhere
    # real (typed CA PVs), and the profile's HEX_SIM StaticPathProvider points at
    # a valid local dir, so report the path as present.
    for name, chan in typed.items():
        if name.endswith("FilePathExists_RBV"):
            chan._data["value"] = 1  # "On" (ChannelEnum value has no setter)
    return typed


def main():
    ap = argparse.ArgumentParser(description="Unified HEX sim IOC.")
    ap.add_argument(
        "--kinetix-ids",
        nargs="*",
        default=["1", "3"],
        help="Kinetix-Det ids to serve with a real typed PV set (default 1 3)",
    )
    ap.add_argument(
        "--motor-pvs",
        nargs="*",
        default=DEFAULT_MOTOR_PVS,
        help="Motor record PV namespaces to EXCLUDE from blackhole (served by the "
        "dedicated motor IOC). Default: the tomography rotation stage MC:5-Ax:4.",
    )
    ap.add_argument(
        "--kinetix-overlay-ids",
        nargs="*",
        default=["1"],
        help="Kinetix-Det ids that are REAL AreaDetector IOCs (frame tier) "
        "needing only the Kinetix-personality overlay PVs the real IOC lacks "
        "(cam1:ReadoutPortIdx). No CA conflict: the real IOC never answers "
        "searches for these. Default: 1.",
    )
    args = ap.parse_args()

    typed = build_detector_pvdb(args.kinetix_ids)
    for i in args.kinetix_overlay_ids:
        typed.update(build_kinetix_overlay_pvdb(f"XF:27ID1-BI{{Kinetix-Det:{i}}}"))

    # Tell blackhole to stay silent for the motor namespace(s) so the dedicated
    # motor IOC (own CA port) answers those searches without conflict. Must be
    # set BEFORE importing spoof_beamline.
    excludes = os.environ.get("BLACKHOLE_EXCLUDE_PREFIXES", "").split()
    excludes += [record_exclude_prefix(pv) for pv in args.motor_pvs]
    os.environ["BLACKHOLE_EXCLUDE_PREFIXES"] = " ".join(excludes)
    from spoof_beamline import BlackholeIOC  # noqa: E402  (after env set)

    ioc = BlackholeIOC()  # ioc.pvdb is a fabricating ReallyDefaultDict
    ioc.pvdb.update(typed)  # seed exact typed detector PVs (override fabrication)
    print(
        f"[hex-sim-ioc] serving {len(typed)} typed detector PVs "
        f"(motors excluded -> dedicated motor IOC) "
        f"+ blackhole fallback on 127.0.0.1 (Ctrl-C to stop)"
    )
    run(ioc.pvdb, interfaces=["127.0.0.1"])


if __name__ == "__main__":
    main()
