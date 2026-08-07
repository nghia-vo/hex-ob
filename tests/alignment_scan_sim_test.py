"""
Functional test: alignment_scan against the simulated HEX beamline.

Runs the real plan with the real devices from lib/ against
hxm_program/simulated_beamlines/HEX (Kinetix-Det:1 = real AreaDetector IOC
with the Kinetix personality; rotation stage = sim motor IOC; sample_x and
the shutter command PVs are fabricated by the blackhole IOC), then asserts:

  - the run completed (stop doc, exit_status success);
  - one 'primary' event per projection angle, num_flats 'flat' events;
  - the HDF file landed under the sim storage tree with the expected
    number of frames.

Prerequisites: sim up (scripts/up_all.sh) and the loopback EPICS client env
(scripts/env.sh) sourced.  Run from the hex-ob root:

    source ~/git_projects/hxm_program/simulated_beamlines/HEX/scripts/env.sh
    cd hex-profile-collection && \
    PYTHONPATH=.. pixi run -e terminal python ../tests/alignment_scan_sim_test.py

Safety: refuses to run unless EPICS_CA_ADDR_LIST is loopback-only — the PV
names are the real beamline names; this must never touch a real gateway.
"""

import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# --------------------------------------------------------------------------
# Loopback-only guard (same rule as the sim's own clients)
# --------------------------------------------------------------------------
addr_list = os.environ.get("EPICS_CA_ADDR_LIST", "")
addrs = addr_list.split()
if not addrs or any(not a.startswith("127.0.0.1") for a in addrs):
    sys.exit(
        f"REFUSING to run: EPICS_CA_ADDR_LIST={addr_list!r} is not loopback-only.\n"
        "Source simulated_beamlines/HEX/scripts/env.sh first."
    )
os.environ.setdefault("EPICS_CA_AUTO_ADDR_LIST", "NO")

# --------------------------------------------------------------------------
# CA context setup — MUST run on the main thread before anything else CA-ish.
#
# Instantiating a RunEngine (in this env) imports pyepics; ophyd-async's CA
# backend then attaches every worker thread to pyepics' "initial context"
# (_use_pyepics_context_if_imported).  If pyepics never actually created that
# context, the attach corrupts aioca's own context in the RunEngine's loop
# thread and every ophyd-async connect times out.  The profile collection
# avoids this by accident (its classic-ophyd motors do CA on the main thread
# first); standalone scripts must do it explicitly.
# --------------------------------------------------------------------------
import epics.ca

epics.ca.initialize_libca()

from bluesky import RunEngine
from ophyd_async.epics.core import epics_signal_rw
from ophyd_async.plan_stubs import ensure_connected

from lib.detectors import make_kinetix
from lib.motors import rot_stage, sample_x
from plans.tomography import alignment_scan

# Sim storage tree: /nsls2/... inside the kinetix IOC container is
# /tmp/hex-sim-data/nsls2/... on the host (sentinel proposal pass-000000).
OUTPUT_DIR = "/nsls2/data/hex/proposals/2026-2/pass-000000/tomography/alignment/scan_sim_test"
HOST_OUTPUT_DIR = Path("/tmp/hex-sim-data") / OUTPUT_DIR.lstrip("/")
NUM_PROJECTIONS = 5
NUM_FLATS = 2


def main() -> None:
    RE = RunEngine({})

    # Shutter command PVs (fabricated by the sim's blackhole IOC).
    ph_open_cmd = epics_signal_rw(int, "XF:27IDA-PPS{L1-S1}Cmd:Opn-Cmd", name="ph_open_cmd")
    ph_close_cmd = epics_signal_rw(int, "XF:27IDA-PPS{L1-S1}Cmd:Cls-Cmd", name="ph_close_cmd")

    kinetix1 = make_kinetix(1)
    RE(ensure_connected(kinetix1, rot_stage, sample_x, ph_open_cmd, ph_close_cmd))
    print("All devices connected.")

    docs: list[tuple[str, dict]] = []
    RE.subscribe(lambda name, doc: docs.append((name, doc)))

    RE(
        alignment_scan(
            kinetix1,
            rot_stage,
            sample_x,
            ph_open_cmd,
            ph_close_cmd,
            output_dir=OUTPUT_DIR,
            exposure_time=0.05,
            num_projections=NUM_PROJECTIONS,
            start_angle=0.0,
            stop_angle=90.0,
            scan_velocity=30.0,
            flat_x_offset=1.0,
            num_flats=NUM_FLATS,
            md={"purpose": "hex-ob sim verification"},
        )
    )

    # -- assertions ---------------------------------------------------------
    stop_docs = [d for n, d in docs if n == "stop"]
    assert len(stop_docs) == 1, f"expected 1 stop doc, got {len(stop_docs)}"
    assert stop_docs[0]["exit_status"] == "success", stop_docs[0]
    print("PASS  run completed (exit_status=success)")

    desc_by_uid = {d["uid"]: d["name"] for n, d in docs if n == "descriptor"}
    events = Counter(desc_by_uid[d["descriptor"]] for n, d in docs if n == "event")
    assert events["primary"] == NUM_PROJECTIONS, f"primary events: {events}"
    assert events["flat"] == NUM_FLATS, f"flat events: {events}"
    print(f"PASS  event streams (primary={NUM_PROJECTIONS}, flat={NUM_FLATS})")

    hdf_files = sorted(HOST_OUTPUT_DIR.glob("*.h5")) + sorted(HOST_OUTPUT_DIR.glob("*.hdf*"))
    assert hdf_files, f"no HDF file under {HOST_OUTPUT_DIR}"
    expected_frames = NUM_PROJECTIONS + NUM_FLATS
    try:
        import h5py

        with h5py.File(hdf_files[0], "r") as f:
            n_frames = f["/entry/data/data"].shape[0]
        assert n_frames == expected_frames, (
            f"expected {expected_frames} frames, HDF has {n_frames}"
        )
        print(f"PASS  HDF file {hdf_files[0].name}: {n_frames} frames")
    except ImportError:
        size = hdf_files[0].stat().st_size
        assert size > 0, f"HDF file {hdf_files[0]} is empty"
        print(f"PASS  HDF file exists ({hdf_files[0].name}, {size} bytes; h5py absent)")

    print("\nALL PASS")


if __name__ == "__main__":
    main()
