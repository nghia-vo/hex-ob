"""
Mock HEX mini-beamline for full-plan RunEngine tests without EPICS.

Builds the device set the tomography plans need — kinetix (mock backends),
rot_stage / sample_x motors, shutter command signals — wired so plans run
END-TO-END:

- each detector ``acquire`` start increments the HDF ``num_captured``
  readback (one frame per trigger), which is what the ophyd-async writer
  logic watches;
- ``file_path_exists`` is seeded True (directory creation is IOC-side in
  real life);
- motor setpoint puts are mirrored to the readback so moves complete.

Not a test itself — imported by the *_mock_test.py scripts.
"""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bluesky.run_engine import RunEngine
from ophyd_async.core import init_devices
from ophyd_async.epics.core import epics_signal_rw
from ophyd_async.epics.motor import Motor

try:
    from ophyd_async.testing import callback_on_mock_put, set_mock_value
except ImportError:
    # ophyd_async.testing imports pytest at import time; the profile pixi env
    # has no pytest, so fall back to the symbols' real home there.
    from ophyd_async.core._mock_signal_utils import (
        callback_on_mock_put,
        set_mock_value,
    )

from lib.detectors import make_kinetix


class MockBeamline:
    """RunEngine + mock devices + captured documents."""

    def __init__(self):
        self.RE = RunEngine({})
        # init_devices discovers devices from the calling frame's LOCALS on
        # exit — bind locals first, then attach to self.
        with init_devices(mock=True):
            detector = make_kinetix(1)
            rot_stage = Motor("XF:MOCK{MC:5-Ax:4}Mtr", name="rot_stage")
            sample_x = Motor("XF:MOCK{SMPL:1-Ax:X1}Mtr", name="sample_x")
            ph_open_cmd = epics_signal_rw(int, "XF:MOCK{Sh}Opn", name="ph_open_cmd")
            ph_close_cmd = epics_signal_rw(int, "XF:MOCK{Sh}Cls", name="ph_close_cmd")
        self.detector = detector
        self.rot_stage = rot_stage
        self.sample_x = sample_x
        self.ph_open_cmd = ph_open_cmd
        self.ph_close_cmd = ph_close_cmd

        set_mock_value(self.detector.hdf.file_path_exists, True)

        self._frames = 0

        def on_capture(value, **kwargs):
            # Like the real HDF plugin: NumCaptured resets when a new capture
            # session starts (otherwise frame counts leak across plans — the
            # live-view restart on unstage also bumps the acquire counter).
            if value:
                self._frames = 0
                set_mock_value(self.detector.hdf.num_captured, 0)

        def on_acquire(value, **kwargs):
            # One frame per acquisition start; stage/stop put False, ignore.
            if value:
                self._frames += 1
                set_mock_value(self.detector.hdf.num_captured, self._frames)

        callback_on_mock_put(self.detector.hdf.capture, on_capture)
        callback_on_mock_put(self.detector.driver.acquire, on_acquire)

        for motor in (self.rot_stage, self.sample_x):
            callback_on_mock_put(
                motor.user_setpoint,
                lambda value, *, m=motor, **kw: set_mock_value(m.user_readback, value),
            )

        self.docs: list[tuple[str, dict]] = []
        self._descriptor_names: dict[str, str] = {}
        self.RE.subscribe(lambda name, doc: self.docs.append((name, doc)))

    def reset(self):
        """Clear captured documents between plans.

        The frame counter is NOT touched here — it resets when the next plan
        starts a new HDF capture (mirroring the real plugin's NumCaptured).
        """
        self.docs.clear()

    def events_by_stream(self) -> dict[str, int]:
        """Event counts per stream name for the captured documents."""
        for name, doc in self.docs:
            if name == "descriptor":
                self._descriptor_names[doc["uid"]] = doc["name"]
        return dict(
            Counter(
                self._descriptor_names[doc["descriptor"]]
                for name, doc in self.docs
                if name == "event"
            )
        )

    def last_stop(self) -> dict:
        stops = [doc for name, doc in self.docs if name == "stop"]
        assert stops, "no stop document captured"
        return stops[-1]
