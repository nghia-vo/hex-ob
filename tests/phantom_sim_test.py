"""
Functional test: the Phantom plan family against the simulated HEX beamline.

Runs the phantom plans with the real devices from lib/ against the sim's
deep Phantom tier — the REAL ADPhantom IOC (CA :5105) driving the
protocol-level camera sim (sim_camera.py) — asserting run success,
per-stream event counts, and a fresh HDF file with the expected frame
total, written by the production plugin chain from the RAM-download path.

Scope (dec:phantom-suite-mock-panda-interim, 2026-08-12): the PandA is an
ophyd-async MOCK until the real phantom PandA configuration is captured at
the beamline (trigger routing, PULSE2+BITS design) — so take_images and
dark_flat_scan (no PandA involvement) run fully real here, and tomo_scan
runs with the panda mocked and the event trigger stood in by a software
trigger.  The PandA-dependent assertions (HDF Angle series, real train
pacing) activate when the design capture lands and the mock is replaced by
the sim PandA.

Prerequisites: sim up (hex-simulated-beamline/scripts/up_all.sh) and the
loopback EPICS client env sourced.  Run from the hex-ob root:

    source hex-simulated-beamline/scripts/env.sh
    pixi run python tests/phantom_sim_test.py

Safety: refuses to run unless EPICS_CA_ADDR_LIST is loopback-only — the PV
names are the real beamline names; this must never touch a real gateway.
"""

import os
import sys
import threading
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
from ophyd_async.core import (
    Device,
    DeviceVector,
    SignalR,
    SignalRW,
    init_devices,
)
from ophyd_async.epics.core import epics_signal_rw
from ophyd_async.fastcs.panda import HDFPanda
from ophyd_async.plan_stubs import ensure_connected

from mock_beamline import callback_on_mock_put, set_mock_value

from lib.detectors import SettablePathProvider
from lib.motors import rot_stage
from lib.phantom import make_phantom
from plans.phantom import dark_flat_scan, take_images, tomo_scan


class _CalcBlock(Device):
    """CALC soft block the tomo plan drives (encoder settle + Angle dataset).

    On the real PandA this arrives via PVI introspection of the loaded
    design; the mock connector only fills declared blocks, so declare it.
    """

    out: SignalR[float]
    out_dataset: SignalRW[str]


class _MockTomoPanda(HDFPanda):
    calc: DeviceVector[_CalcBlock]

BASE = "/nsls2/data/hex/proposals/2026-2/pass-000000/phantom"
TOMO_NUM = 61
TOMO_START, TOMO_STOP = 0.0, 30.0


def host_dir(output_dir: str) -> Path:
    """The host-side view of an in-sim /nsls2 output directory."""
    return Path("/tmp/hex-sim-data") / output_dir.lstrip("/")


def check_hdf(directory: Path, since: float, expected_frames: int, label: str):
    hdf_files = sorted(
        (
            p
            for pattern in ("*.h5", "*.hdf", "*.hdf5")
            for p in directory.glob(pattern)
            if p.stat().st_mtime >= since - 1
        ),
        key=lambda p: p.stat().st_mtime,
    )
    assert hdf_files, f"{label}: no HDF file written under {directory}"
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
    phantom1 = make_phantom()
    # Blackhole PVs first, separately: the phantom's ~50-signal search burst
    # can drown the blackhole's two searches when connected together (the
    # caproto blackhole materializes PVs per search), and the pair then
    # times out.
    RE(ensure_connected(ph_open_cmd, ph_close_cmd))
    RE(ensure_connected(phantom1, rot_stage))

    # The PandA is a MOCK per dec:phantom-suite-mock-panda-interim (its real
    # phantom block design is not yet captured at the beamline). Choreograph
    # only what the plan structurally requires of it: the PCAP arm handshake,
    # and "captured everything" at arm time so its flyer completes. The HDF
    # Angle series and real train pacing are the FULL gate's assertions,
    # deferred until the design capture replaces this mock with the sim
    # PandA.
    panda_pp = SettablePathProvider(filename="panda")
    with init_devices(mock=True):
        panda1 = _MockTomoPanda(
            "XF:27ID1-ES{PANDA:1}:", panda_pp, name="panda1"
        )
    panda1.path_provider = panda_pp

    def _panda_armed(value, **kw):
        set_mock_value(panda1.pcap.active, 1 if value else 0)
        if value:
            # arming resets PCAP's capture count (real PandA semantics);
            # the counter advances when the train fires (the trigger
            # thread below models that).
            set_mock_value(panda1.data.num_captured, 0)

    callback_on_mock_put(panda1.pcap.arm, _panda_armed)
    # The writer's open() checks directory_exists (computed by the real IOC).
    set_mock_value(panda1.data.directory_exists, 1)
    print("All devices connected (PhantomIO + rot_stage real, PandA mock).")

    docs: list[tuple[str, dict]] = []
    RE.subscribe(lambda name, doc: docs.append((name, doc)))

    def events() -> dict[str, int]:
        desc = {d["uid"]: d["name"] for n, d in docs if n == "descriptor"}
        return dict(Counter(desc[d["descriptor"]] for n, d in docs if n == "event"))

    def stop_ok():
        stops = [d for n, d in docs if n == "stop"]
        assert stops and stops[-1]["exit_status"] == "success", stops[-1:]

    # -- take_images ---------------------------------------------------------
    docs.clear()
    out = f"{BASE}/raw_data/take_images_sim_test"
    t0 = time.time()
    RE(take_images(
        phantom1, ph_open_cmd=ph_open_cmd, ph_close_cmd=ph_close_cmd,
        output_dir=out, exposure_time=0.005, acquire_period=0.02,
        num_images=10,
    ))
    stop_ok()
    assert events() == {"primary": 1}, events()
    check_hdf(host_dir(out), t0, 10, "take_images")

    # -- dark_flat_scan ------------------------------------------------------
    docs.clear()
    out = f"{BASE}/raw_data/dark_flat_sim_test"
    t0 = time.time()
    RE(dark_flat_scan(
        phantom1, ph_open_cmd, ph_close_cmd,
        output_dir=out, exposure_time=0.005, acquire_period=0.02,
        num_dark=3, num_flat=5,
    ))
    stop_ok()
    assert events() == {"flat": 1, "dark": 1}, events()
    # flat and dark are separate captures -> two HDF files in one dir; the
    # newest holds the dark frames, so check both counts explicitly.
    files = sorted(host_dir(out).glob("*.h5"), key=lambda p: p.stat().st_mtime)
    recent = [p for p in files if p.stat().st_mtime >= t0 - 1]
    assert len(recent) >= 2, f"dark_flat: expected 2 HDF files, got {len(recent)}"
    import h5py

    counts = []
    for p in recent[-2:]:
        with h5py.File(p, "r") as f:
            counts.append(f["/entry/data/data"].shape[0])
    assert sorted(counts) == [3, 5], f"dark_flat: frame counts {counts}, expected 3+5"
    print(f"PASS  dark_flat_scan: two HDF captures with {counts} frames")

    # -- tomo_scan (real phantom + rot_stage, mock PandA) --------------------
    # The event trigger normally arrives from the PandA train as the stage
    # crosses the start angle; with the PandA mocked, this thread stands in
    # for that one wire: fire the software trigger when the camera is armed
    # and the sweep has reached the start angle (the PCOMP semantic).
    fired = threading.Event()
    stop_thread = threading.Event()

    def _train_stand_in():
        from epics import caget, caput

        P = "XF:27ID1-ES{Phantom-Det:1}cam1:"
        RBV = "XF:27IDF-OP:1{MC:5-Ax:4}Mtr.RBV"
        while not stop_thread.is_set():
            time.sleep(0.05)
            armed = caget(P + "State_RBV.B2", timeout=2)  # waiting_for_trigger
            angle = caget(RBV, timeout=2)
            if armed == 1 and angle is not None and angle >= TOMO_START:
                caput(P + "SendSoftwareTrigger", 1, wait=False)
                # ... and the same train drives PCAP: the mock panda
                # "captures" its per-pulse rows.
                set_mock_value(panda1.data.num_captured, TOMO_NUM)
                fired.set()
                return

    docs.clear()
    out = f"{BASE}/raw_data/tomo_sim_test"
    t0 = time.time()
    trigger_thread = threading.Thread(target=_train_stand_in, daemon=True)
    trigger_thread.start()
    try:
        RE(tomo_scan(
            phantom1, panda1, rot_stage,
            ph_open_cmd=ph_open_cmd, ph_close_cmd=ph_close_cmd,
            output_dir=out, exposure_time=0.005,
            num_projections=TOMO_NUM,
            start_deg=TOMO_START, stop_deg=TOMO_STOP,
        ))
    finally:
        stop_thread.set()
    assert fired.is_set(), "train stand-in never fired the event trigger"
    stop_ok()
    streams = {d["name"] for n, d in docs if n == "descriptor"}
    assert "tomo" in streams, streams
    check_hdf(host_dir(out), t0, TOMO_NUM, "tomo_scan (proj)")
    print("PASS  tomo_scan (mock-PandA interim: Angle series deferred to the "
          "full gate per dec:phantom-suite-mock-panda-interim)")

    print("\nALL PHANTOM SIM TESTS PASS (interim scope: mock PandA)")


if __name__ == "__main__":
    main()
