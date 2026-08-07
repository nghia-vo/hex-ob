"""Per-detector caproto sim for the HEX Kinetix (tomography).

Presents the *exact* typed PV set of ophyd-async's `KinetixDetector` (cam1 + the
HDF1 plugin, incl. the Kinetix `TriggerMode`/`ReadoutPortIdx` StrictEnums and the
bool AD PVs the generic blackhole gets wrong) so `HEXKinetixDetector` connects
and reads/writes for real. Frames are not produced (that's the full-fidelity
ADSimDetector route); this is the device-development ("quick connect") tier.

Run in an env with ophyd-async + caproto (the hex-profile-collection `terminal`
pixi env):
    pixi run -e terminal python iocs/sim_devices/kinetix_sim.py [--prefix ...] [--det-id 1]
"""

import argparse
from pathlib import Path

from _ophyd_async_sim import run_devices_sim
from ophyd_async.core import StaticPathProvider, UUIDFilenameProvider
from ophyd_async.epics.adcore import ADHDFDataLogic, ADWriterFactory, NDFileHDF5IO
from ophyd_async.epics.adkinetix import KinetixDetector


def build_kinetix(prefix: str) -> KinetixDetector:
    path_provider = StaticPathProvider(UUIDFilenameProvider(), Path("/tmp"))
    return KinetixDetector(
        prefix,
        ADWriterFactory(
            writer_cls=NDFileHDF5IO,
            writer_suffix="HDF1:",
            writer_name="hdf",
            datakey_suffix="",
            array_description=None,
            data_logic_factory=lambda writer, desc, driver, plugins: ADHDFDataLogic(
                array_description=desc,
                path_provider=path_provider,
                driver=driver,
                writer=writer,
                plugins=list(plugins),
                datakey_suffix="",
            ),
        ),
        name="kinetix_sim",
    )


def build_kinetix_overlay_pvdb(prefix: str) -> dict:
    """Kinetix-personality OVERLAY for a real AreaDetector IOC (frame tier).

    The ADSimDetector-based frame-tier IOC serves ~all of the typed
    ``KinetixDetector``'s PV demand except the Kinetix-specific PVs. This
    returns caproto channels for exactly those — served from sim_ioc with NO
    CA conflict, because the real IOC never answers searches for them.
    (The other personality gap, cam1:TriggerMode's enum choices, is fixed on
    the real IOC's own record by iocs/kinetix/init_kinetix.py.)
    """
    from caproto import ChannelEnum
    from ophyd_async.epics.adkinetix import KinetixReadoutMode

    readout = ChannelEnum(
        value=0, enum_strings=[e.value for e in KinetixReadoutMode]
    )
    return {
        prefix + "cam1:ReadoutPortIdx": readout,
        prefix + "cam1:ReadoutPortIdx_RBV": readout,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Kinetix caproto sim (typed PV set).")
    ap.add_argument(
        "--det-ids",
        nargs="+",
        default=["1", "3"],
        help="Kinetix-Det ids to serve (default: 1 3 — both HEX cameras)",
    )
    args = ap.parse_args()
    devices = [build_kinetix(f"XF:27ID1-BI{{Kinetix-Det:{i}}}") for i in args.det_ids]
    run_devices_sim(devices, label="Kinetix Det:" + ",".join(args.det_ids))
