"""
Phantom (Vision Research VEO) acquisition plans — HXM-1288 P-series.

Ports of hex-acq-pyepics/techniques/tomography/phantom/ onto the migrated
PhantomDetector (lib/phantom.py).  ``check_alignment`` is the shared
analysis script (analysis/check_alignment.py); ``time_series_scan`` is
pending the PandA PULSE2/BITS soft-start wiring decision.
"""

from .configure import (
    black_reference,
    configure_phantom,
    soft_triggered_capture,
    wait_for_armed,
)
from .dark_flat_scan import dark_flat_scan
from .take_images import take_images
from .tomo_scan import tomo_scan

__all__ = [
    "black_reference",
    "configure_phantom",
    "dark_flat_scan",
    "soft_triggered_capture",
    "take_images",
    "tomo_scan",
    "wait_for_armed",
]
