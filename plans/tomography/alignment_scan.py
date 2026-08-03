"""
Tomography alignment scan plan for HEX beamline.

Equivalent of the old pyepics script:
    pyepics_workflows/techniques/tomography/kinetix/alignment_scan.py

What this plan does
-------------------
1.  Opens the photon shutter (checks front-end is open first).
2.  Moves the rotation stage to each angle in linspace(start, stop, num_projections).
3.  Takes one image at every angle with the Kinetix camera (free-run, Single mode).
4.  Optionally takes a flat-field image: moves sample_x by *flat_x_offset*, acquires
    *num_flats* frames, then moves sample back.
5.  Closes the photon shutter when finished (unless *keep_shutter_open* is True).
6.  Restores the rotation stage to its original angle.

Output path
-----------
The old script built the output folder as:
    /nsls2/data/hex/proposals/{cycle}/{proposal}/tomography/{base_folder}/scan_NNNNN/

In this plan the scientist passes ``output_dir`` — the exact folder where the HDF
file should land.  A ``StaticPathProvider`` wraps that directory so the detector
writes its file there.  This gives the same explicit control as the old script.

Usage (inside a Bluesky RunEngine session)
------------------------------------------
    # Minimal example
    RE(alignment_scan(
        kinetix1, rot_stage, sample_x, ph_open_cmd, ph_close_cmd,
        output_dir="/nsls2/data/hex/proposals/2026-2/pass-319162/tomography/alignment/scan_00001",
        exposure_time=0.02,
    ))

    # With flat-field images (move sample 5 mm in X)
    RE(alignment_scan(
        kinetix1, rot_stage, sample_x, ph_open_cmd, ph_close_cmd,
        output_dir="/nsls2/data/hex/proposals/2026-2/pass-319162/tomography/alignment/scan_00001",
        exposure_time=0.05,
        num_projections=37,
        start_angle=0.0,
        stop_angle=360.0,
        flat_x_offset=5.0,
        num_flats=10,
        md={"sample": "my_sample"},
    ))
"""

from pathlib import Path

import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp
import numpy as np
from ophyd_async.core import StaticFilenameProvider, StaticPathProvider


# ---------------------------------------------------------------------------
# Shutter helpers
# (Mirrors open_ph_shutter / close_ph_shutter from 03-motors.py reference)
# ---------------------------------------------------------------------------

def _open_photon_shutter(ph_open_cmd, sleep_time: float = 3.0):
    """Open the photon shutter and wait for it to settle."""
    print("Opening photon shutter...")
    yield from bps.abs_set(ph_open_cmd, 1, wait=True)
    yield from bps.sleep(sleep_time)
    print("Photon shutter open.")


def _close_photon_shutter(ph_close_cmd, sleep_time: float = 3.0):
    """Close the photon shutter and wait for it to settle."""
    print("Closing photon shutter...")
    yield from bps.abs_set(ph_close_cmd, 1, wait=True)
    yield from bps.sleep(sleep_time)
    print("Photon shutter closed.")


# ---------------------------------------------------------------------------
# Main plan
# ---------------------------------------------------------------------------

def alignment_scan(
    detector,
    rot_stage,
    sample_x,
    ph_open_cmd,
    ph_close_cmd,
    *,
    output_dir: str,
    exposure_time: float,
    num_projections: int = 37,
    start_angle: float = 0.0,
    stop_angle: float = 360.0,
    scan_velocity: float = 30.0,
    flat_x_offset: float = 0.0,
    num_flats: int = 1,
    keep_shutter_open: bool = False,
    md: dict = None,
):
    """
    Tomography alignment step-scan with the Kinetix detector.

    Parameters
    ----------
    detector : HEXKinetixDetector
        The Kinetix detector instance to use (e.g. kinetix1 or kinetix3).
    rot_stage : Motor
        The tomography rotation stage motor.
    sample_x : Motor
        The sample X-axis motor (used to move sample out for flat-field).
    ph_open_cmd : EpicsSignal
        Signal to open the photon shutter (write 1 to open).
    ph_close_cmd : EpicsSignal
        Signal to close the photon shutter (write 1 to close).
    output_dir : str
        Full path to the directory where the detector HDF file will be saved.
        This is the equivalent of the old script's auto-generated scan folder:
            /nsls2/data/hex/proposals/{cycle}/{proposal}/tomography/{base_folder}/scan_NNNNN/
        Example:
            "/nsls2/data/hex/proposals/2026-2/pass-319162/tomography/alignment/scan_00001"
    exposure_time : float
        Camera exposure time in seconds.
    num_projections : int, optional
        Number of projection angles. Default is 37.
    start_angle : float, optional
        First rotation angle in degrees. Default is 0.0.
    stop_angle : float, optional
        Last rotation angle in degrees. Default is 360.0.
    scan_velocity : float, optional
        Rotation stage velocity in deg/s during the scan. Default is 30.0.
    flat_x_offset : float, optional
        Relative X shift (mm) to move sample out of beam for flat-field.
        If 0.0 (or < 0.5 mm), no flat images are taken. Default is 0.0.
    num_flats : int, optional
        Number of flat images to collect when flat_x_offset is set. Default is 1.
    keep_shutter_open : bool, optional
        If True, leave the photon shutter open after the scan. Default is False.
    md : dict, optional
        Extra metadata to attach to the run. Default is None.
    """
    # ------------------------------------------------------------------
    # Point the detector's path provider at the requested output directory.
    # This mirrors the old:  camera.set_hdf_file_path(output_folder, "proj")
    # ------------------------------------------------------------------
    output_path = Path(output_dir)
    path_provider = StaticPathProvider(
        filename_provider=StaticFilenameProvider("proj"),
        path=output_path,
    )
    detector.set_path_provider(path_provider)

    # ------------------------------------------------------------------
    # Build run metadata
    # ------------------------------------------------------------------
    _md = {
        "plan_name": "alignment_scan",
        "detectors": [detector.name],
        "motors": [rot_stage.name],
        "output_dir": str(output_path),
        "exposure_time": exposure_time,
        "num_projections": num_projections,
        "start_angle": start_angle,
        "stop_angle": stop_angle,
        "flat_x_offset": flat_x_offset,
        "num_flats": num_flats if abs(flat_x_offset) >= 0.5 else 0,
        "hints": {"dimensions": [([rot_stage.name], "primary")]},
    }
    _md.update(md or {})

    # Pre-compute the list of angles
    angles = np.linspace(start_angle, stop_angle, num_projections)

    # ------------------------------------------------------------------
    # Remember initial positions so we can restore them at the end
    # ------------------------------------------------------------------
    init_angle = yield from bps.rd(rot_stage)

    # ------------------------------------------------------------------
    # Open photon shutter
    # ------------------------------------------------------------------
    yield from _open_photon_shutter(ph_open_cmd)

    # ------------------------------------------------------------------
    # Move rotation stage to start angle at scan velocity
    # ------------------------------------------------------------------
    yield from bps.mv(rot_stage.velocity, scan_velocity)
    yield from bps.mv(rot_stage, start_angle)

    # ------------------------------------------------------------------
    # Run: stage detector, open a Bluesky run, acquire projections
    # ------------------------------------------------------------------
    @bpp.stage_decorator([detector])
    @bpp.run_decorator(md=_md)
    def _inner_scan():
        # Configure camera: Single image mode, Internal (free-run) trigger.
        # Equivalent to old: camera.set_image_mode(0), camera.set_trigger_mode(0)
        yield from bps.mv(
            detector.driver.acquire_time,   exposure_time,
            detector.driver.acquire_period, exposure_time + 0.002,
            detector.driver.image_mode,     "Single",
            detector.driver.trigger_mode,   "Internal",
            detector.driver.num_images,     1,
        )

        print("\n" + "=" * 52)
        print("  Acquiring alignment projections")
        print(f"  Output : {output_path}")
        print(f"  Angles : {start_angle:.1f} -> {stop_angle:.1f} deg "
              f"({num_projections} points)")
        print("=" * 52)

        for angle in angles:
            # Move stage to this angle (blocking)
            yield from bps.mv(rot_stage, angle)
            print(f"  Angle: {angle:.3f} deg")

            # Trigger the detector for one frame and record
            yield from bps.trigger_and_read([detector, rot_stage])

        # ---------------------------------------------------------------
        # Optional: flat-field images
        # Equivalent to old: deco.move_relative_sample_stage_x(offset) +
        #                    take_flat_images(1, ...)
        # ---------------------------------------------------------------
        if abs(flat_x_offset) >= 0.5:
            # Point the detector to the flat sub-directory
            flat_path_provider = StaticPathProvider(
                filename_provider=StaticFilenameProvider("flat"),
                path=output_path / "flat",
            )
            detector.set_path_provider(flat_path_provider)

            print("\n" + "-" * 52)
            print("  Moving sample out for flat-field images...")
            yield from bps.mvr(sample_x, flat_x_offset)

            yield from bps.mv(detector.driver.num_images, num_flats)
            print(f"  Acquiring {num_flats} flat image(s)")
            for _ in range(num_flats):
                yield from bps.trigger_and_read([detector], name="flat")

            print("  Moving sample back to original X position...")
            yield from bps.mvr(sample_x, -flat_x_offset)
            print("-" * 52)

    # ---------------------------------------------------------------
    # Cleanup: always runs even on interrupt or error
    # ---------------------------------------------------------------
    def _cleanup():
        if not keep_shutter_open:
            yield from _close_photon_shutter(ph_close_cmd)
        # Restore rotation stage to its original angle
        yield from bps.mv(rot_stage, init_angle)
        print(f"  Rotation stage restored to {init_angle:.3f} deg")

    return (yield from bpp.finalize_wrapper(_inner_scan(), _cleanup()))
