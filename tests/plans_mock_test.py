"""
Full-plan mock tests: every tomography plan runs END-TO-END under the
RunEngine against the mock beamline (tests/mock_beamline.py) — zero EPICS,
zero containers — asserting per-stream event counts, run success, and the
restore behaviors the plans promise.

Run from the hex-ob root (same path CI uses):
    pixi run test-mock
Or directly:
    python tests/plans_mock_test.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mock_beamline import MockBeamline

from plans.tomography import (
    alignment_scan,
    scan_1d,
    take_dark_flat,
    take_radiograph,
    tomo_flyscan,
)

OUT = "/tmp/hex-ob-mock/scan_test"


def main() -> None:
    bl = MockBeamline()

    # -- take_radiograph ----------------------------------------------------
    bl.reset()
    bl.RE(take_radiograph(
        bl.detector, bl.ph_open_cmd, bl.ph_close_cmd,
        output_dir=OUT, exposure_time=0.01, num_images=3,
    ))
    assert bl.last_stop()["exit_status"] == "success"
    assert bl.events_by_stream() == {"primary": 3}, bl.events_by_stream()
    print("PASS  take_radiograph (3 primary)")

    # -- scan_1d ------------------------------------------------------------
    bl.reset()
    bl.RE(scan_1d(
        bl.detector, bl.rot_stage, bl.ph_open_cmd, bl.ph_close_cmd,
        output_dir=OUT, exposure_time=0.01,
        start=0.0, stop=9.0, num_points=4,
    ))
    assert bl.last_stop()["exit_status"] == "success"
    assert bl.events_by_stream() == {"primary": 4}, bl.events_by_stream()
    # motor left at stop, like the original script
    final = asyncio.run(bl.rot_stage.user_readback.get_value())
    assert final == 9.0, f"scan_1d should leave motor at stop, got {final}"
    print("PASS  scan_1d (4 primary, motor left at stop)")

    # -- take_dark_flat -----------------------------------------------------
    bl.reset()
    bl.RE(take_dark_flat(
        bl.detector, bl.sample_x, bl.ph_open_cmd, bl.ph_close_cmd,
        output_dir=OUT, exposure_time=0.01,
        num_dark=2, num_flat=3, flat_x_offset=5.0,
    ))
    assert bl.last_stop()["exit_status"] == "success"
    assert bl.events_by_stream() == {"dark": 2, "flat": 3}, bl.events_by_stream()
    # sample restored after flats
    sx = asyncio.run(bl.sample_x.user_readback.get_value())
    assert sx == 0.0, f"sample_x should be restored to 0, got {sx}"
    print("PASS  take_dark_flat (2 dark + 3 flat, sample restored)")

    # -- alignment_scan -----------------------------------------------------
    bl.reset()
    bl.RE(alignment_scan(
        bl.detector, bl.rot_stage, bl.sample_x,
        bl.ph_open_cmd, bl.ph_close_cmd,
        output_dir=OUT, exposure_time=0.01,
        num_projections=5, start_angle=0.0, stop_angle=90.0,
        flat_x_offset=1.0, num_flats=2,
    ))
    assert bl.last_stop()["exit_status"] == "success"
    assert bl.events_by_stream() == {"primary": 5, "flat": 2}, bl.events_by_stream()
    # rotation restored to initial position (was 9.0 after scan_1d above)
    rot = asyncio.run(bl.rot_stage.user_readback.get_value())
    assert rot == 9.0, f"alignment_scan should restore rotation, got {rot}"
    print("PASS  alignment_scan (5 primary + 2 flat, rotation restored)")

    # -- tomo_flyscan (structural: mock device tree + validation) -----------
    # The fly path (PandA kickoff/complete + PCAP) is functionally verified
    # against the simulated beamline (tests/tomo_flyscan_sim_test.py); under
    # mock we verify the panda device tree builds and argument validation.
    def start_plan(gen):
        """Drive the generator to its first message (plan bodies don't run
        until first next()), then shut it down."""
        msg = next(gen)
        try:
            gen.close()
        except RuntimeError:
            pass  # finalizer yields cleanup messages during close
        return msg

    msg = start_plan(tomo_flyscan(
        bl.detector, bl.panda, bl.rot_stage,
        ph_open_cmd=bl.ph_open_cmd, ph_close_cmd=bl.ph_close_cmd,
        output_dir=OUT, exposure_time=0.015, num_projections=61,
        start_deg=0, stop_deg=30,
    ))
    assert msg is not None
    for bad_kwargs, expect in [
        (dict(num_projections=1), "num_projections"),
        (dict(num_projections=61, exposure_time=0.0), "exposure_time"),
        (dict(num_projections=61, ph_open_cmd=None, ph_close_cmd=None), "use_shutter"),
    ]:
        kwargs = dict(
            output_dir=OUT, exposure_time=0.015,
            ph_open_cmd=bl.ph_open_cmd, ph_close_cmd=bl.ph_close_cmd,
        )
        kwargs.update(bad_kwargs)
        try:
            start_plan(tomo_flyscan(bl.detector, bl.panda, bl.rot_stage, **kwargs))
        except ValueError as exc:
            assert expect in str(exc), exc
        else:
            raise AssertionError(f"expected ValueError for {bad_kwargs}")
    print("PASS  tomo_flyscan (structural: builds with mock panda; validation raises)")

    print("\nALL PASS")


if __name__ == "__main__":
    main()
