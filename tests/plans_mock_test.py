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
    enable_flat_correction,
    reset_detector,
    run_multiple_2d_scans,
    run_multiple_scans,
    scan_1d,
    take_dark_flat,
    take_radiograph,
    tomo_flyscan,
    tomo_flyscan_average,
)
from plans.tomography.tomo_flyscan_average import (
    configure_averaging,
    restore_averaging,
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

    # -- batch runners (orchestration with stub sub-plans) ------------------
    import bluesky.plan_stubs as bps

    calls: list[tuple[str, str, int]] = []

    def stub(kind):
        def _plan(output_dir, index):
            calls.append((kind, output_dir.rsplit("/", 1)[-1], index))
            yield from bps.null()
        return _plan

    # motor mode, dark/flat every 2 (no initial), motor restored
    calls.clear()
    bl.RE(bps.mv(bl.sample_x, 0.0))
    bl.RE(run_multiple_scans(
        bl.detector, bl.panda, bl.rot_stage, bl.sample_x,
        output_base_dir="/tmp/hex-ob-mock/batch",
        exposure_time=0.01, num_projections=61, flat_x_offset=5.0,
        scan_motor=bl.sample_x, start=-1.0, stop=1.0, num_points=3,
        dark_flat_every=2,
        ph_open_cmd=bl.ph_open_cmd, ph_close_cmd=bl.ph_close_cmd,
        scan_plan=stub("scan"), dark_flat_plan=stub("df"),
    ))
    assert calls == [
        ("scan", "scan_00001", 1),
        ("scan", "scan_00002", 2),
        ("df", "dark_flat_00001", 1),
        ("scan", "scan_00003", 3),
    ], calls
    sx = asyncio.run(bl.sample_x.user_readback.get_value())
    assert sx == 0.0, f"scan motor should be restored, got {sx}"
    print("PASS  run_multiple_scans (motor mode: order, cadence, restore)")

    # time mode, initial dark/flat only
    calls.clear()
    bl.RE(run_multiple_scans(
        bl.detector, bl.panda, bl.rot_stage, bl.sample_x,
        output_base_dir="/tmp/hex-ob-mock/batch",
        exposure_time=0.01, num_projections=61, flat_x_offset=5.0,
        num_scans=2, sleep_time=0.01, dark_flat_every=-1,
        ph_open_cmd=bl.ph_open_cmd, ph_close_cmd=bl.ph_close_cmd,
        scan_plan=stub("scan"), dark_flat_plan=stub("df"),
    ))
    assert [c[0] for c in calls] == ["df", "scan", "scan"], calls
    print("PASS  run_multiple_scans (time mode: initial dark/flat only)")

    # 2D grid: dark/flat per row, scans row-major, both motors restored
    calls.clear()
    bl.RE(bps.mv(bl.rot_stage, 0.0, bl.sample_x, 0.0))
    bl.RE(run_multiple_2d_scans(
        bl.detector, bl.panda, bl.rot_stage, bl.sample_x,
        motor1=bl.sample_x, start1=-1.0, stop1=1.0, num_points1=2,
        motor2=bl.rot_stage, start2=0.0, stop2=5.0, num_points2=2,
        output_base_dir="/tmp/hex-ob-mock/grid",
        exposure_time=0.01, num_projections=61, flat_x_offset=5.0,
        ph_open_cmd=bl.ph_open_cmd, ph_close_cmd=bl.ph_close_cmd,
        scan_plan=stub("scan"), dark_flat_plan=stub("df"),
    ))
    assert [c[0] for c in calls] == ["df", "scan", "scan", "df", "scan", "scan"], calls
    assert asyncio.run(bl.sample_x.user_readback.get_value()) == 0.0
    assert asyncio.run(bl.rot_stage.user_readback.get_value()) == 0.0
    print("PASS  run_multiple_2d_scans (grid order, dark/flat per row, restore)")

    # -- averaging wiring (full mock run of configure/restore) --------------
    from mock_beamline import set_mock_value

    set_mock_value(bl.detector.hdf.nd_array_port, "KINETIX1")
    set_mock_value(bl.detector.proc.port_name, "PROC1")

    saved = {}

    def _wire():
        saved["port"] = yield from configure_averaging(bl.detector, 4)
    bl.RE(_wire())
    assert saved["port"] == "KINETIX1"
    assert asyncio.run(bl.detector.proc.num_filter.get_value()) == 4
    assert asyncio.run(bl.detector.proc.enable_filter.get_value()) == "Enable"
    assert asyncio.run(bl.detector.proc.nd_array_port.get_value()) == "KINETIX1"
    assert asyncio.run(bl.detector.hdf.nd_array_port.get_value()) == "PROC1"
    bl.RE(restore_averaging(bl.detector, saved["port"]))
    assert asyncio.run(bl.detector.proc.enable_filter.get_value()) == "Disable"
    assert asyncio.run(bl.detector.hdf.nd_array_port.get_value()) == "KINETIX1"
    print("PASS  averaging wiring (configure routes HDF->PROC1, restore reverts)")

    # -- tomo_flyscan_average (structural + validation) ---------------------
    msg = start_plan(tomo_flyscan_average(
        bl.detector, bl.panda, bl.rot_stage,
        frames_to_average=4,
        ph_open_cmd=bl.ph_open_cmd, ph_close_cmd=bl.ph_close_cmd,
        output_dir=OUT, exposure_time=0.015, num_projections=61,
        start_deg=0, stop_deg=30,
    ))
    assert msg is not None
    try:
        start_plan(tomo_flyscan_average(
            bl.detector, bl.panda, bl.rot_stage,
            frames_to_average=1,
            ph_open_cmd=bl.ph_open_cmd, ph_close_cmd=bl.ph_close_cmd,
            output_dir=OUT, exposure_time=0.015, num_projections=61,
        ))
    except ValueError as exc:
        assert "frames_to_average" in str(exc)
    else:
        raise AssertionError("frames_to_average=1 should raise")
    print("PASS  tomo_flyscan_average (structural; validation raises)")

    # -- reset_detector (full mock run) -------------------------------------
    set_mock_value(bl.detector.hdf.nd_array_port, "PROC1")   # pretend broken
    set_mock_value(bl.detector.pva.nd_array_port, "PROC1")
    bl.RE(reset_detector(bl.detector, panda=bl.panda, rot_stage=bl.rot_stage))
    assert asyncio.run(bl.detector.hdf.nd_array_port.get_value()) == "TRANS1"
    assert asyncio.run(bl.detector.pva.nd_array_port.get_value()) == "TRANS1"
    assert asyncio.run(bl.detector.proc.enable_filter.get_value()) == "Disable"
    assert asyncio.run(bl.rot_stage.velocity.get_value()) == 30.0
    print("PASS  reset_detector (ports to default, filter off, velocity reset)")

    # -- enable_flat_correction (full mock run, enable then disable) --------
    set_mock_value(bl.detector.pva.nd_array_port, "KINETIX1")
    set_mock_value(bl.detector.proc.port_name, "PROC1")
    set_mock_value(bl.detector.proc.valid_flat_field, "Valid")
    bl.RE(enable_flat_correction(
        bl.detector, bl.sample_x, bl.ph_open_cmd, bl.ph_close_cmd,
        exposure_time=0.01, flat_x_offset=2.0, enable=True, settle_time=0.01,
    ))
    assert asyncio.run(bl.detector.pva.nd_array_port.get_value()) == "PROC1"
    assert asyncio.run(bl.detector.proc.enable_flat_field.get_value()) == "Enable"
    assert asyncio.run(bl.sample_x.user_readback.get_value()) == 0.0  # restored
    bl.RE(enable_flat_correction(
        bl.detector, bl.sample_x, bl.ph_open_cmd, bl.ph_close_cmd,
        exposure_time=0.01, flat_x_offset=2.0, enable=False,
    ))
    assert asyncio.run(bl.detector.pva.nd_array_port.get_value()) == "TRANS1"
    assert asyncio.run(bl.detector.proc.enable_flat_field.get_value()) == "Disable"
    print("PASS  enable_flat_correction (enable wires PVA->PROC1 + captures; disable restores)")

    print("\nALL PASS")


if __name__ == "__main__":
    main()
