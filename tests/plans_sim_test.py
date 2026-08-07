"""
Functional test: the step-scan plan family against the simulated HEX beamline.

Runs take_radiograph, scan_1d, and take_dark_flat (alignment_scan has its own
runner) with the real devices from lib/ against hex-simulated-beamline —
Kinetix-Det:1 real AD IOC frame tier, rot_stage/sample_x FakeMotor records,
blackhole shutter cmd PVs — asserting run success, per-stream event counts,
and a fresh HDF file with the expected frame total for each plan.

Prerequisites: sim up (hex-simulated-beamline/scripts/up_all.sh) and the
loopback EPICS client env sourced.  Run from the hex-ob root:

    source hex-simulated-beamline/scripts/env.sh
    cd hex-profile-collection && \
    PYTHONPATH=.. pixi run -e terminal python ../tests/plans_sim_test.py

Safety: refuses to run unless EPICS_CA_ADDR_LIST is loopback-only — the PV
names are the real beamline names; this must never touch a real gateway.
"""

import os
import sys
import time
from collections import Counter
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

# CA context setup — see alignment_scan_sim_test.py for the full story.
import epics.ca

epics.ca.initialize_libca()

from bluesky import RunEngine
from ophyd_async.epics.core import epics_signal_rw
from ophyd_async.plan_stubs import ensure_connected

from lib.detectors import make_kinetix
from lib.motors import rot_stage, sample_x
from plans.tomography import scan_1d, take_dark_flat, take_radiograph

BASE = "/nsls2/data/hex/proposals/2026-2/pass-000000/tomography"
HOST_BASE = Path("/tmp/hex-sim-data") / BASE.lstrip("/")


def check_hdf(host_dir: Path, since: float, expected_frames: int, label: str):
    hdf_files = sorted(
        (
            p
            for pattern in ("*.h5", "*.hdf", "*.hdf5")
            for p in host_dir.glob(pattern)
            if p.stat().st_mtime >= since - 1
        ),
        key=lambda p: p.stat().st_mtime,
    )
    assert hdf_files, f"{label}: no HDF file written under {host_dir}"
    try:
        import h5py

        with h5py.File(hdf_files[-1], "r") as f:
            n = f["/entry/data/data"].shape[0]
        assert n == expected_frames, f"{label}: expected {expected_frames} frames, got {n}"
        print(f"PASS  {label}: HDF {hdf_files[-1].name} has {n} frames")
    except ImportError:
        assert hdf_files[-1].stat().st_size > 0
        print(f"PASS  {label}: HDF exists ({hdf_files[-1].name}; h5py absent)")


def main() -> None:
    RE = RunEngine({})
    ph_open_cmd = epics_signal_rw(int, "XF:27IDA-PPS{L1-S1}Cmd:Opn-Cmd", name="ph_open_cmd")
    ph_close_cmd = epics_signal_rw(int, "XF:27IDA-PPS{L1-S1}Cmd:Cls-Cmd", name="ph_close_cmd")
    kinetix1 = make_kinetix(1)
    RE(ensure_connected(kinetix1, rot_stage, sample_x, ph_open_cmd, ph_close_cmd))
    print("All devices connected.")

    docs: list[tuple[str, dict]] = []
    RE.subscribe(lambda name, doc: docs.append((name, doc)))

    def events() -> dict[str, int]:
        desc = {d["uid"]: d["name"] for n, d in docs if n == "descriptor"}
        return dict(Counter(desc[d["descriptor"]] for n, d in docs if n == "event"))

    def stop_ok():
        stops = [d for n, d in docs if n == "stop"]
        assert stops and stops[-1]["exit_status"] == "success", stops[-1:]

    # -- take_radiograph ----------------------------------------------------
    docs.clear()
    out = f"{BASE}/radiograph/scan_sim_test"
    t0 = time.time()
    RE(take_radiograph(
        kinetix1, ph_open_cmd, ph_close_cmd,
        output_dir=out, exposure_time=0.05, num_images=3,
    ))
    stop_ok()
    assert events() == {"primary": 3}, events()
    check_hdf(Path("/tmp/hex-sim-data") / out.lstrip("/"), t0, 3, "take_radiograph")

    # -- scan_1d ------------------------------------------------------------
    docs.clear()
    out = f"{BASE}/raw_data/scan_1d_sim_test"
    t0 = time.time()
    RE(scan_1d(
        kinetix1, rot_stage, ph_open_cmd, ph_close_cmd,
        output_dir=out, exposure_time=0.05,
        start=0.0, stop=30.0, num_points=4,
    ))
    stop_ok()
    assert events() == {"primary": 4}, events()
    check_hdf(Path("/tmp/hex-sim-data") / out.lstrip("/"), t0, 4, "scan_1d")

    # -- take_dark_flat -----------------------------------------------------
    docs.clear()
    out = f"{BASE}/raw_data/dark_flat_sim_test"
    t0 = time.time()
    RE(take_dark_flat(
        kinetix1, sample_x, ph_open_cmd, ph_close_cmd,
        output_dir=out, exposure_time=0.05,
        num_dark=2, num_flat=3, flat_x_offset=2.0,
    ))
    stop_ok()
    assert events() == {"dark": 2, "flat": 3}, events()
    check_hdf(Path("/tmp/hex-sim-data") / out.lstrip("/"), t0, 5, "take_dark_flat")

    print("\nALL PASS")


if __name__ == "__main__":
    main()
