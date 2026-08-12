"""
Phantom still-image acquisition plan for HEX beamline.

Equivalent of the old pyepics script:
    hex-acq-pyepics/techniques/tomography/phantom/take_images.py

The camera free-runs into cine RAM (FREE-RUN sync), a software event
trigger anchors the capture, the camera records *num_images* post-trigger
frames, and the RAM window downloads through the HDF plugin chain — all of
which is the device's arm→trigger→count→download flow (lib/phantom.py).

Differences from the original (flagged for beamline review)
-----------------------------------------------------------
- The event trigger is sent FIRST and the *num_images* frames follow it
  (post-trigger window); the legacy script recorded first, soft-triggered
  twice at the end, and downloaded pre-trigger frames from RAM.  Same frame
  count, different timing convention (see plans/phantom/configure.py).
- One soft trigger, not the legacy belt-and-braces two.
- No .nxs metadata sidecar — metadata lives in the run documents.

Usage
-----
    RE(take_images(
        phantom1, ph_open_cmd=ph_open_cmd, ph_close_cmd=ph_close_cmd,
        output_dir=".../phantom/raw_data/scan_00001",
        exposure_time=0.005, acquire_period=0.02, num_images=100,
    ))
"""

import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp
from ophyd_async.core import DetectorTrigger, TriggerInfo

from lib.detectors import set_output_dir
from lib.shutter import close_photon_shutter, open_photon_shutter

from .configure import (
    MAX_ACQUIRE_PERIOD_INTERNAL,
    black_reference,
    configure_phantom,
    soft_triggered_capture,
)


def take_images(
    phantom,
    *,
    output_dir: str,
    exposure_time: float,
    acquire_period: float,
    num_images: int,
    use_shutter: bool = True,
    ph_open_cmd=None,
    ph_close_cmd=None,
    md: dict | None = None,
):
    """
    Collect *num_images* Phantom frames through one soft-triggered cine
    capture (``primary`` stream).

    Parameters
    ----------
    phantom : PhantomDetector
        Built by ``lib.phantom.make_phantom``.
    output_dir : str
        Directory for the HDF file (the old script's scan_NNNNN folder).
    exposure_time : float
        Exposure per frame, seconds.
    acquire_period : float
        Frame period, seconds; must exceed *exposure_time* and stay within
        the camera's sync ceiling (legacy 1/24 s).
    num_images : int
        Post-trigger frames to record and download.
    use_shutter : bool, optional
        Drive the photon shutter (needs ph_open_cmd / ph_close_cmd).
    md : dict, optional
        Extra run metadata.
    """
    if num_images < 1:
        raise ValueError(f"num_images must be >= 1, got {num_images}")
    if acquire_period > MAX_ACQUIRE_PERIOD_INTERNAL:
        raise ValueError(
            f"acquire_period {acquire_period} exceeds the camera's maximum "
            f"{MAX_ACQUIRE_PERIOD_INTERNAL:.6g} s (legacy 1/24 rate floor)"
        )
    if exposure_time >= acquire_period:
        raise ValueError(
            f"exposure_time {exposure_time} must be smaller than "
            f"acquire_period {acquire_period}"
        )
    if use_shutter and (ph_open_cmd is None or ph_close_cmd is None):
        raise ValueError(
            "use_shutter=True needs ph_open_cmd and ph_close_cmd "
            "(or pass use_shutter=False)."
        )

    output_path = set_output_dir(phantom, output_dir, "img")

    _md = {
        "plan_name": "phantom_take_images",
        "detectors": [phantom.name],
        "output_dir": str(output_path),
        "exposure_time": exposure_time,
        "acquire_period": acquire_period,
        "num_images": num_images,
    }
    _md.update(md or {})

    def _scan():
        effective = yield from configure_phantom(
            phantom, post_trig_frames=num_images, acquire_period=acquire_period
        )
        yield from black_reference(phantom)
        if use_shutter:
            yield from open_photon_shutter(ph_open_cmd)

        @bpp.stage_decorator([phantom])
        @bpp.run_decorator(md=dict(_md, num_images=effective))
        def _inner():
            yield from bps.prepare(
                phantom,
                TriggerInfo(
                    trigger=DetectorTrigger.INTERNAL,
                    livetime=exposure_time,
                    deadtime=acquire_period - exposure_time,
                    number_of_events=1,
                    collections_per_event=effective,
                ),
                wait=True,
            )
            yield from soft_triggered_capture(phantom, "primary")

        yield from _inner()

    def _cleanup():
        if use_shutter and ph_close_cmd is not None:
            yield from close_photon_shutter(ph_close_cmd)

    return (yield from bpp.finalize_wrapper(_scan(), _cleanup()))
