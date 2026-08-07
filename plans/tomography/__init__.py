"""
HEX beamline tomography plans — one module per legacy pyepics script,
detector(s) passed as arguments (configure, don't copy, per detector).

Exports
-------
alignment_scan  : alignment step-scan with optional flats
take_dark_flat  : dark + flat-field acquisition
take_radiograph : N frames at a fixed position
scan_1d         : 1-D motor step-scan, one image per point
tomo_flyscan    : hardware-triggered fly-scan tomography (PandA)
"""

from .alignment_scan import alignment_scan
from .scan_1d import scan_1d
from .take_dark_flat import take_dark_flat
from .take_radiograph import take_radiograph
from .tomo_flyscan import tomo_flyscan

__all__ = ["alignment_scan", "take_dark_flat", "take_radiograph", "scan_1d", "tomo_flyscan"]
