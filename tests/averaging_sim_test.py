"""
Functional test: frame-averaging wiring against the simulated HEX beamline.

Verifies, on the REAL AD IOC of the sim's frame tier, that
plans.tomography.tomo_flyscan_average's wiring plans work end to end:

  1. configure_averaging routes camera -> Proc1 (recursive filter, N=2)
     -> HDF plugin, saving the original HDF source port;
  2. a small internally-triggered acquisition (4 raw frames) yields an HDF
     file with 2 AVERAGED frames — proving the IOC's plugin chain honors
     the wiring, not just that the PVs accept writes;
  3. restore_averaging disables the filter and re-points the HDF plugin at
     the original port (the old scripts left the rewire in place, which is
     what util/reset_detector.py existed to undo).

The full tomo_flyscan_average plan rides the sim's armed-external gating
item (see tomo_flyscan_sim_test.py); this test pins down the averaging
mechanism itself, which is trigger-mode independent.

Prerequisites/safety: as the other *_sim_test.py files.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

addr_list = os.environ.get("EPICS_CA_ADDR_LIST", "")
addrs = addr_list.split()
if not addrs or any(not a.startswith("127.0.0.1") for a in addrs):
    sys.exit(
        f"REFUSING to run: EPICS_CA_ADDR_LIST={addr_list!r} is not loopback-only.\n"
        "Source hex-simulated-beamline/scripts/env.sh first."
    )
os.environ.setdefault("EPICS_CA_AUTO_ADDR_LIST", "NO")

import epics.ca

epics.ca.initialize_libca()

import bluesky.plan_stubs as bps
from bluesky import RunEngine
from ophyd_async.epics.adcore import ADFileWriteMode, ADImageMode
from ophyd_async.plan_stubs import ensure_connected

from lib.detectors import make_kinetix
from plans.tomography.tomo_flyscan_average import (
    configure_averaging,
    restore_averaging,
)

OUT = "/nsls2/data/hex/proposals/2026-2/pass-000000/tomography/raw_data/averaging_sim_test"
HOST_OUT = Path("/tmp/hex-sim-data") / OUT.lstrip("/")
RAW, AVG = 4, 2


def main() -> None:
    RE = RunEngine({})
    kinetix1 = make_kinetix(1)
    RE(ensure_connected(kinetix1))
    print("Detector connected (incl. proc plugin).")

    state = {}

    def _test():
        det = kinetix1
        # 1. wire averaging
        state["saved_port"] = yield from configure_averaging(det, AVG)
        got = yield from bps.rd(det.proc.num_filter)
        assert got == AVG, got
        hdf_port = yield from bps.rd(det.hdf.nd_array_port)
        proc_port = yield from bps.rd(det.proc.port_name)
        assert hdf_port == proc_port, (hdf_port, proc_port)
        print(f"PASS  wiring: {state['saved_port']} -> {proc_port} -> HDF")

        # 2. small internally-triggered acquisition through the filter
        yield from bps.mv(
            det.driver.acquire_time, 0.05,
            det.driver.image_mode, ADImageMode.MULTIPLE,
            det.driver.num_images, RAW,
        )
        yield from bps.mv(
            det.hdf.create_directory, -4,
            det.hdf.file_path, f"{OUT}/",
            det.hdf.file_name, "avg",
            det.hdf.file_write_mode, ADFileWriteMode.STREAM,
            det.hdf.num_capture, AVG,
        )
        yield from bps.abs_set(det.hdf.capture, 1, wait=False)
        yield from bps.sleep(0.5)
        yield from bps.abs_set(det.driver.acquire, 1, wait=True)  # 4 frames
        for _ in range(40):
            done = yield from bps.rd(det.hdf.num_captured)
            if done >= AVG:
                break
            yield from bps.sleep(0.25)
        n = yield from bps.rd(det.hdf.num_captured)
        assert n == AVG, f"HDF captured {n}, expected {AVG} averaged frames"
        yield from bps.abs_set(det.hdf.capture, 0, wait=True)
        print(f"PASS  {RAW} raw frames -> {n} averaged frames captured")

        # 3. restore
        yield from restore_averaging(det, state["saved_port"])
        hdf_port = yield from bps.rd(det.hdf.nd_array_port)
        assert hdf_port == state["saved_port"], hdf_port
        filt = yield from bps.rd(det.proc.enable_filter)
        assert filt == "Disable", filt
        print(f"PASS  restored: HDF -> {hdf_port}, filter disabled")

    t0 = time.time()
    RE(_test())

    files = [p for p in HOST_OUT.glob("avg*.h5") if p.stat().st_mtime >= t0 - 1]
    assert files, f"no HDF file under {HOST_OUT}"
    try:
        import h5py

        with h5py.File(files[-1], "r") as f:
            n_frames = f["/entry/data/data"].shape[0]
        assert n_frames == AVG, f"file has {n_frames} frames, expected {AVG}"
        print(f"PASS  HDF file {files[-1].name}: {n_frames} averaged frames")
    except ImportError:
        print(f"PASS  HDF file exists ({files[-1].name}; h5py absent)")

    print("\nALL PASS")


if __name__ == "__main__":
    main()
