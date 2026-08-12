"""
Phantom dark + flat-field acquisition plan for HEX beamline.

Equivalent of the old pyepics script:
    hex-acq-pyepics/techniques/tomography/phantom/dark_flat_scan.py

Two soft-triggered cine captures in one run: flats with the shutter open
(``flat`` stream), then darks with it closed (``dark`` stream) — the legacy
order.  Each capture is the device's arm→trigger→count→download flow, with
a black reference before each phase like the original.

Differences from the original (flagged for beamline review)
-----------------------------------------------------------
- Post-trigger window per phase (event first, frames after) instead of the
  legacy record-first / trigger-last pre-trigger download — same counts,
  different timing convention (see plans/phantom/configure.py).
- One run with ``flat`` / ``dark`` event streams and per-phase HDF files
  (``flat``/``dark`` filename), not separate scripts' folders; no .nxs
  sidecar — metadata lives in the run documents.  (Same conventions as the
  kinetix ``take_dark_flat`` port.)
- One soft trigger per phase, not the legacy two.

Usage
-----
    RE(dark_flat_scan(
        phantom1, ph_open_cmd=ph_open_cmd, ph_close_cmd=ph_close_cmd,
        output_dir=".../phantom/raw_data/scan_00001",
        exposure_time=0.005, acquire_period=0.02,
        num_dark=20, num_flat=50,
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


def dark_flat_scan(
    phantom,
    ph_open_cmd,
    ph_close_cmd,
    *,
    output_dir: str,
    exposure_time: float,
    acquire_period: float,
    num_dark: int = 20,
    num_flat: int = 50,
    keep_shutter_open: bool = False,
    md: dict | None = None,
):
    """
    Collect Phantom flat-field then dark images.

    Parameters
    ----------
    phantom : PhantomDetector
        Built by ``lib.phantom.make_phantom``.
    ph_open_cmd / ph_close_cmd : signal
        Photon-shutter command signals (write 1 to actuate).
    output_dir : str
        Directory for the HDF files (the old script's scan_NNNNN folder).
    exposure_time / acquire_period : float
        Per-frame exposure and period, seconds (period within the legacy
        1/24 s ceiling).
    num_dark / num_flat : int, optional
        Frame counts; either may be 0 to skip that phase. Defaults 20 / 50.
    keep_shutter_open : bool, optional
        Leave the shutter open afterwards. Default False.
    md : dict, optional
        Extra run metadata.
    """
    if num_dark < 0 or num_flat < 0:
        raise ValueError("num_dark / num_flat must be >= 0")
    if num_dark == 0 and num_flat == 0:
        raise ValueError("nothing to do: num_dark and num_flat are both 0")
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

    _md = {
        "plan_name": "phantom_dark_flat_scan",
        "detectors": [phantom.name],
        "output_dir": output_dir,
        "exposure_time": exposure_time,
        "acquire_period": acquire_period,
        "num_dark": num_dark,
        "num_flat": num_flat,
    }
    _md.update(md or {})

    def _phase(stream_name: str, num: int):
        """Configure + black-ref + capture one phase (flat or dark)."""
        set_output_dir(phantom, output_dir, stream_name)
        effective = yield from configure_phantom(
            phantom, post_trig_frames=num, acquire_period=acquire_period
        )
        yield from black_reference(phantom)
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
        yield from soft_triggered_capture(phantom, stream_name)

    @bpp.stage_decorator([phantom])
    @bpp.run_decorator(md=_md)
    def _inner():
        if num_flat > 0:
            print("-" * 52)
            print(f"  Shutter open — acquiring {num_flat} flat image(s)")
            yield from open_photon_shutter(ph_open_cmd)
            yield from _phase("flat", num_flat)

        if num_dark > 0:
            print("-" * 52)
            print(f"  Shutter closed — acquiring {num_dark} dark image(s)")
            yield from close_photon_shutter(ph_close_cmd)
            yield from _phase("dark", num_dark)

    def _cleanup():
        if not keep_shutter_open:
            yield from close_photon_shutter(ph_close_cmd)

    return (yield from bpp.finalize_wrapper(_inner(), _cleanup()))
