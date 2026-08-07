"""
Functional test: tomo_flyscan against the simulated HEX beamline.

Exercises the real fly path end-to-end: the sim PandA (real PandABlocks
engine running the production Tomo_radio_1_config design) armed via PCOMP at
the start-angle encoder count, PULSE1 emitting the time train, PCAP capturing
the per-trigger Angle; the motor->INENC bridge feeding the encoder; the
Kinetix frame tier producing real frames.  This is the D-0014 equivalence
surface — the same outputs the pyepics oracle produces (proj HDF frames +
panda HDF ``Angle``):

  - run completes; detector and PandA both captured num_projections;
  - the detector HDF holds num_projections frames;
  - the PandA HDF ``Angle`` dataset has num_projections values, strictly
    increasing, starting ~start_deg and ending ~stop_deg.

Note: the sim frame tier free-runs when armed (trigger-mode semantics are
cosmetic in the sim — PROGRESS §11), exactly like the oracle's camera leg;
frame/angle COUNTS and the angle series are the equivalence claims here.
True per-pulse frame sync is a real-beamline check (batched session).

Prerequisites: sim up (hex-simulated-beamline/scripts/up_all.sh) and the
loopback EPICS client env sourced.  Run from the hex-ob root:

    source hex-simulated-beamline/scripts/env.sh
    cd hex-profile-collection && \
    PYTHONPATH=.. pixi run -e terminal python ../tests/tomo_flyscan_sim_test.py

Safety: refuses to run unless EPICS_CA_ADDR_LIST is loopback-only.
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

# CA context setup — see alignment_scan_sim_test.py for the full story.
import epics.ca

epics.ca.initialize_libca()

from bluesky import RunEngine
from ophyd_async.epics.core import epics_signal_rw
from ophyd_async.plan_stubs import ensure_connected

from lib.detectors import make_kinetix
from lib.motors import rot_stage
from lib.panda import make_panda
from plans.tomography import tomo_flyscan

NUM = 61
START, STOP = 0.0, 30.0
STEP = (STOP - START) / (NUM - 1)
OUT = "/nsls2/data/hex/proposals/2026-2/pass-000000/tomography/raw_data/flyscan_sim_test"
HOST_OUT = Path("/tmp/hex-sim-data") / OUT.lstrip("/")


def newest(host_dir: Path, keyword: str, since: float) -> Path:
    files = sorted(
        (
            p
            for pattern in ("*.h5", "*.hdf", "*.hdf5")
            for p in host_dir.glob(pattern)
            if keyword in p.name and p.stat().st_mtime >= since - 1
        ),
        key=lambda p: p.stat().st_mtime,
    )
    assert files, f"no fresh '{keyword}' HDF under {host_dir}"
    return files[-1]


def find_angle_dataset(h5file):
    """Locate the Angle dataset regardless of exact group layout."""
    found = []

    def visitor(name, obj):
        if hasattr(obj, "shape") and "angle" in name.lower():
            found.append(name)

    h5file.visititems(visitor)
    assert found, f"no Angle dataset in {h5file.filename}: {list(h5file.keys())}"
    return h5file[found[0]]


def main() -> None:
    RE = RunEngine({})
    ph_open_cmd = epics_signal_rw(int, "XF:27IDA-PPS{L1-S1}Cmd:Opn-Cmd", name="ph_open_cmd")
    ph_close_cmd = epics_signal_rw(int, "XF:27IDA-PPS{L1-S1}Cmd:Cls-Cmd", name="ph_close_cmd")
    kinetix1 = make_kinetix(1)
    panda1 = make_panda(1)
    RE(ensure_connected(kinetix1, panda1, rot_stage, ph_open_cmd, ph_close_cmd))
    print("All devices connected (panda over PVA).")

    docs: list[tuple[str, dict]] = []
    RE.subscribe(lambda name, doc: docs.append((name, doc)))

    t0 = time.time()
    try:
        RE(tomo_flyscan(
        kinetix1, panda1, rot_stage,
        ph_open_cmd=ph_open_cmd, ph_close_cmd=ph_close_cmd,
        output_dir=OUT,
        exposure_time=0.015,
        num_projections=NUM,
        start_deg=START, stop_deg=STOP,
        lead_angle=5.0,
        ))
    except Exception as exc:
        if "Kickoff requested" in str(exc):
            sys.exit(
                "KNOWN SIM GAP (armed-external free-run): the sim frame tier "
                "free-runs when armed, so frames leak in before kickoff and "
                "ophyd-async's range check refuses. At the real beamline an "
                "EXTERNAL_EDGE-armed camera waits for TTL and this passes. "
                "Sim increment tracked: gate frame production on the PandA "
                "pulse train when TriggerMode is external.\n"
                f"(underlying: {exc})"
            )
        raise

    stops = [d for n, d in docs if n == "stop"]
    assert stops and stops[-1]["exit_status"] == "success", stops[-1:]
    print("PASS  run completed (exit_status=success)")

    stream_names = {d["name"] for n, d in docs if n == "descriptor"}
    assert "tomo" in stream_names, stream_names
    print("PASS  'tomo' stream declared")

    import h5py

    with h5py.File(newest(HOST_OUT, "proj", t0), "r") as f:
        n_frames = f["/entry/data/data"].shape[0]
    assert n_frames == NUM, f"expected {NUM} frames, detector HDF has {n_frames}"
    print(f"PASS  detector HDF: {n_frames} frames")

    with h5py.File(newest(HOST_OUT, "panda", t0), "r") as f:
        angles = find_angle_dataset(f)[:].squeeze()
    assert angles.shape[0] == NUM, f"expected {NUM} angles, got {angles.shape}"
    diffs = angles[1:] - angles[:-1]
    assert (diffs > 0).all(), "Angle series is not strictly increasing"
    assert abs(angles[0] - START) <= 2 * STEP, f"first angle {angles[0]} vs start {START}"
    assert abs(angles[-1] - STOP) <= 2 * STEP, f"last angle {angles[-1]} vs stop {STOP}"
    print(f"PASS  PandA Angle: {angles.shape[0]} values, "
          f"{angles[0]:.3f} -> {angles[-1]:.3f} deg, strictly increasing")

    print("\nALL PASS")


if __name__ == "__main__":
    main()
