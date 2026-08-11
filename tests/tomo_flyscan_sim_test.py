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
  - the PandA HDF ``Angle`` dataset has num_projections values,
    non-decreasing (the sim's motor->INENC bridge updates slower than the
    pulse train, so consecutive pulses can capture the same angle; a real
    encoder would be strictly increasing), starting ~start_deg and ending
    ~stop_deg.

The sim frame tier free-runs when armed (trigger-mode semantics are
cosmetic in the driver — PROGRESS §11), so this test SPAWNS the opt-in
armed_gate_bridge, which holds the armed camera and gates the plugin chain
until the PULSE1 train fires — the sim increment that closed the
"armed-external free-run" gap. reset_detector runs first so the HDF plugin
consumes through the gated TRANS1 routing. True per-pulse frame sync is
still a real-beamline check (batched session).

Prerequisites: sim up (hex-simulated-beamline/scripts/up_all.sh) and the
loopback EPICS client env sourced.  Run from the hex-ob root:

    source hex-simulated-beamline/scripts/env.sh
    cd hex-profile-collection && \
    PYTHONPATH=.. pixi run -e terminal python ../tests/tomo_flyscan_sim_test.py

Safety: refuses to run unless EPICS_CA_ADDR_LIST is loopback-only.
"""

import os
import subprocess
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
from plans.tomography import reset_detector, tomo_flyscan

BRIDGE = (Path(__file__).resolve().parents[1]
          / "hex-simulated-beamline/iocs/panda/armed_gate_bridge.py")
TOOLENV_PY = (Path(__file__).resolve().parents[1]
              / "hex-simulated-beamline/.toolenv/bin/python")

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


def main(phase: str = "plain") -> None:
    RE = RunEngine({})
    ph_open_cmd = epics_signal_rw(int, "XF:27IDA-PPS{L1-S1}Cmd:Opn-Cmd", name="ph_open_cmd")
    ph_close_cmd = epics_signal_rw(int, "XF:27IDA-PPS{L1-S1}Cmd:Cls-Cmd", name="ph_close_cmd")
    kinetix1 = make_kinetix(1)
    panda1 = make_panda(1)
    RE(ensure_connected(kinetix1, panda1, rot_stage, ph_open_cmd, ph_close_cmd))
    print("All devices connected (panda over PVA).")

    docs: list[tuple[str, dict]] = []
    RE.subscribe(lambda name, doc: docs.append((name, doc)))

    # Known-good routing (HDF/PVA via TRANS1 — the port the bridge gates).
    RE(reset_detector(kinetix1, panda=panda1, rot_stage=rot_stage))

    # The armed-external gate bridge, for the duration of the scan.
    py = str(TOOLENV_PY) if TOOLENV_PY.exists() else sys.executable
    bridge_log_path = "/tmp/hex-armed-gate-bridge.log"
    bridge_log = open(bridge_log_path, "w")
    bridge = subprocess.Popen(
        [py, str(BRIDGE)], stdout=bridge_log, stderr=subprocess.STDOUT,
        text=True,
    )
    for _ in range(100):
        time.sleep(0.2)
        if "armed_gate_bridge up" in Path(bridge_log_path).read_text():
            print(f"bridge up (log: {bridge_log_path})")
            break
    else:
        bridge.terminate()
        sys.exit(f"armed_gate_bridge did not start — see {bridge_log_path}")

    if phase == "average":
        try:
            _phase_average(RE, kinetix1, panda1, ph_open_cmd, ph_close_cmd)
        finally:
            # An orphaned bridge keeps driving Acquire and doubles the
            # hold/release cycles of every later run — never leave one.
            if bridge.poll() is None:
                bridge.terminate()
        return

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
        bridge.terminate()
        if "Kickoff requested" in str(exc):
            sys.exit(
                "Armed-external gap resurfaced DESPITE the gate bridge — "
                "frames leaked before kickoff. Check the bridge output and "
                "the HDF plugin routing (must consume via TRANS1).\n"
                f"(underlying: {exc})"
            )
        raise
    finally:
        if bridge.poll() is None:
            bridge.terminate()

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
    # Non-decreasing, not strictly increasing: the sim's motor->INENC
    # bridge updates at ~10 Hz while the pulse train samples at 50 Hz, so
    # consecutive pulses can capture the same angle (plateaus). A real
    # encoder is continuous and would be strictly increasing.
    assert (diffs >= 0).all(), "Angle series decreased"
    assert diffs.max() > 0, "Angle series never advanced"
    # Endpoint tolerance = the sim encoder's step: the motor->INENC bridge
    # updates at ~10 Hz, so at scan velocity the first value PAST the start
    # threshold (where PCOMP fires) can overshoot by up to velocity/10 Hz
    # (~2.5 deg here) — a real, continuous encoder fires within counts.
    ENC_TOL = 4.0
    assert abs(angles[0] - START) <= ENC_TOL, f"first angle {angles[0]} vs start {START}"
    assert abs(angles[-1] - STOP) <= ENC_TOL, f"last angle {angles[-1]} vs stop {STOP}"
    print(f"PASS  PandA Angle: {angles.shape[0]} values, "
          f"{angles[0]:.3f} -> {angles[-1]:.3f} deg, non-decreasing")

    print("\nPLAIN PHASE PASS")


def _phase_average(RE, kinetix1, panda1, ph_open_cmd, ph_close_cmd):
    # Frame-averaged fly scan through the same gate.  Runs in its OWN
    # process (see the driver): consecutive fly scans in one session show
    # a one-frame plugin-state interplay — flagged for the beamline batch,
    # where run_multiple_scans does consecutive scans for real.
    from plans.tomography import tomo_flyscan_average

    docs: list[tuple[str, dict]] = []
    RE.subscribe(lambda name, doc: docs.append((name, doc)))

    AVG_N, AVG_M = 30, 2
    out_avg = OUT + "_avg"
    t0 = time.time()
    RE(tomo_flyscan_average(
        kinetix1, panda1, rot_stage,
        frames_to_average=AVG_M,
        ph_open_cmd=ph_open_cmd, ph_close_cmd=ph_close_cmd,
        output_dir=out_avg,
        exposure_time=0.015,
        num_projections=AVG_N,
        start_deg=START, stop_deg=STOP,
        lead_angle=5.0,
    ))
    stops = [d for n, d in docs if n == "stop"]
    assert stops and stops[-1]["exit_status"] == "success", stops[-1:]
    import h5py

    host_avg = Path("/tmp/hex-sim-data") / out_avg.lstrip("/")
    with h5py.File(newest(host_avg, "proj", t0), "r") as f:
        n_frames = f["/entry/data/data"].shape[0]
    assert n_frames == AVG_N, f"expected {AVG_N} averaged frames, got {n_frames}"
    with h5py.File(newest(host_avg, "panda", t0), "r") as f:
        n_angles = find_angle_dataset(f)[:].squeeze().shape[0]
    assert n_angles == AVG_N * AVG_M, (
        f"expected {AVG_N * AVG_M} raw-pulse angles, got {n_angles}"
    )
    print(f"PASS  tomo_flyscan_average: {AVG_N} averaged frames, "
          f"{n_angles} raw-pulse angles")
    print("\nAVERAGE PHASE PASS")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        for phase in ("plain", "average"):
            print(f"\n=== phase: {phase} ===")
            result = subprocess.run([sys.executable, __file__, phase])
            if result.returncode != 0:
                sys.exit(f"FAILED phase: {phase}")
        print("\nALL PASS")
