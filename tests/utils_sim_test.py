"""
Functional test: recovery + flat-correction utility plans against the sim.

1. reset_detector: deliberately mis-wires the plugin routing (HDF/PVA ->
   PROC1, filter on), then asserts the plan restores default routing
   (TRANS1 on Det:1), disables the filter, resets the stage velocity and
   restarts the live view — on the REAL AD IOC + FakeMotor + real PandA.
2. enable_flat_correction: enable path captures a flat-field reference into
   the Proc plugin (ValidFlatField -> Valid on the real IOC), routes the
   live view through Proc, and restores the sample position; disable path
   reverts the routing.
3. analysis/check_alignment.py --info: loads the alignment_scan output file
   produced by the sim run (5 proj + 2 flats in one HDF) and reports the
   split — the loader half of the offline analysis, dependency-light.

Each phase runs in its OWN interpreter (the script re-execs itself):
long standalone CA sessions mixing aioca + the pyepics initial context
degrade after heavy signal churn (reads on healthy PVs start timing out —
same fragility family as the initialize_libca gotcha), and phase isolation
sidesteps that while keeping one test file.

Prerequisites/safety: as the other *_sim_test.py files.
"""

import os
import subprocess
import sys
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
from ophyd_async.epics.core import epics_signal_rw
from ophyd_async.plan_stubs import ensure_connected

from lib.detectors import make_kinetix
from lib.motors import rot_stage, sample_x
from lib.panda import make_panda
from plans.tomography import enable_flat_correction, reset_detector

ALIGN_OUT = Path("/tmp/hex-sim-data/nsls2/data/hex/proposals/2026-2/pass-000000"
                 "/tomography/alignment/scan_sim_test")


def run_phase(phase: str) -> None:
    RE = RunEngine({})
    ph_open_cmd = epics_signal_rw(int, "XF:27IDA-PPS{L1-S1}Cmd:Opn-Cmd", name="ph_open_cmd")
    ph_close_cmd = epics_signal_rw(int, "XF:27IDA-PPS{L1-S1}Cmd:Cls-Cmd", name="ph_close_cmd")
    kinetix1 = make_kinetix(1)
    panda1 = make_panda(1)
    RE(ensure_connected(kinetix1, panda1, rot_stage, sample_x, ph_open_cmd, ph_close_cmd))
    print("All devices connected.")

    if phase == "flat":
        _phase_flat(RE, kinetix1, ph_open_cmd, ph_close_cmd)
        return
    # -- 1. reset_detector --------------------------------------------------
    def _break_things():
        yield from bps.mv(kinetix1.hdf.nd_array_port, "PROC1")
        yield from bps.mv(kinetix1.pva.nd_array_port, "PROC1")
        yield from bps.mv(kinetix1.proc.enable_filter, "Enable")
        yield from bps.mv(rot_stage.velocity, 5.0)

    RE(_break_things())
    RE(reset_detector(kinetix1, panda=panda1, rot_stage=rot_stage))

    def _check_reset():
        hdf_port = yield from bps.rd(kinetix1.hdf.nd_array_port)
        pva_port = yield from bps.rd(kinetix1.pva.nd_array_port)
        filt = yield from bps.rd(kinetix1.proc.enable_filter)
        velo = yield from bps.rd(rot_stage.velocity)
        acquiring = yield from bps.rd(kinetix1.driver.acquire)
        assert hdf_port == "TRANS1", hdf_port
        assert pva_port == "TRANS1", pva_port
        assert filt == "Disable", filt
        assert velo == 30.0, velo
        assert acquiring, "live view should be running after reset"

    RE(_check_reset())
    print("PASS  reset_detector (routing TRANS1, filter off, velocity 30, live view on)")


def _phase_flat(RE, kinetix1, ph_open_cmd, ph_close_cmd) -> None:
    # -- 2. enable_flat_correction ------------------------------------------
    import asyncio
    x0 = asyncio.run(sample_x.user_readback.get_value())
    RE(enable_flat_correction(
        kinetix1, sample_x, ph_open_cmd, ph_close_cmd,
        exposure_time=0.05, flat_x_offset=2.0, enable=True,
    ))

    def _check_flat_on():
        pva_port = yield from bps.rd(kinetix1.pva.nd_array_port)
        ff = yield from bps.rd(kinetix1.proc.enable_flat_field)
        valid = yield from bps.rd(kinetix1.proc.valid_flat_field)
        assert pva_port == "PROC1", pva_port
        assert ff == "Enable", ff
        assert valid == "Valid", valid

    RE(_check_flat_on())
    x_after = asyncio.run(sample_x.user_readback.get_value())
    assert abs(x_after - x0) < 1e-6, f"sample_x not restored: {x_after} vs {x0}"
    print("PASS  enable_flat_correction (reference Valid on real IOC, PVA->PROC1, sample restored)")

    RE(enable_flat_correction(
        kinetix1, sample_x, ph_open_cmd, ph_close_cmd,
        exposure_time=0.05, flat_x_offset=2.0, enable=False,
    ))

    def _check_flat_off():
        pva_port = yield from bps.rd(kinetix1.pva.nd_array_port)
        ff = yield from bps.rd(kinetix1.proc.enable_flat_field)
        assert pva_port == "TRANS1", pva_port
        assert ff == "Disable", ff

    RE(_check_flat_off())
    print("PASS  enable_flat_correction disable (routing + correction reverted)")


def _phase_loader() -> None:
    # -- 3. check_alignment loader smoke ------------------------------------
    if ALIGN_OUT.exists():
        script = Path(__file__).resolve().parents[1] / "analysis/check_alignment.py"
        result = subprocess.run(
            [sys.executable, str(script), str(ALIGN_OUT), "--num-flats", "2", "--info"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "Projections: (5," in result.stdout, result.stdout
        print("PASS  check_alignment --info splits 5 proj + 2 flats from the sim file")
    else:
        print("SKIP  check_alignment smoke (no alignment_scan sim output present)")


def main() -> None:
    if len(sys.argv) > 1:
        phase = sys.argv[1]
        if phase == "loader":
            _phase_loader()
        else:
            run_phase(phase)
        return
    # Driver: one interpreter per CA-heavy phase (see module docstring).
    for phase in ("reset", "flat"):
        print(f"\n=== phase: {phase} ===")
        result = subprocess.run([sys.executable, __file__, phase])
        if result.returncode != 0:
            sys.exit(f"FAILED phase: {phase}")
    print("\n=== phase: loader ===")
    _phase_loader()
    print("\nALL PASS")


if __name__ == "__main__":
    main()
