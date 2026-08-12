"""
Phantom plan mock tests: take_images, dark_flat_scan and tomo_scan run
END-TO-END under the RunEngine against mock signal backends — zero EPICS,
zero containers.

The mock choreography mirrors the camera's cine flow: Acquire=1 arms
(waiting_for_trigger), the event trigger (software put, or the first sweep
move in tomo — standing in for the PandA train) sets trigger_received and
the post-trigger frame count, and Download=1 ramps download_count plus the
HDF NumCaptured the writer watches.

Run from the hex-ob root (same path CI uses):
    pixi run test-mock
Or directly:
    python tests/phantom_plans_mock_test.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bluesky.run_engine import RunEngine
from collections import Counter
from ophyd_async.core import init_devices
from ophyd_async.epics.core import epics_signal_rw
from ophyd_async.epics.motor import Motor

from mock_beamline import callback_on_mock_put, set_mock_value

from lib.panda import make_panda
from lib.phantom import make_phantom
from plans.phantom import dark_flat_scan, take_images, tomo_scan

OUT = "/tmp/hex-ob-mock/phantom_scan"


class PhantomMockBeamline:
    """RunEngine + mock phantom/panda/motor set + captured documents."""

    def __init__(self):
        self.RE = RunEngine({})
        with init_devices(mock=True):
            phantom = make_phantom()
            panda = make_panda(1)
            rot_stage = Motor("XF:MOCK{MC:5-Ax:4}Mtr", name="rot_stage")
            ph_open_cmd = epics_signal_rw(int, "XF:MOCK{Sh}Opn", name="ph_open_cmd")
            ph_close_cmd = epics_signal_rw(int, "XF:MOCK{Sh}Cls", name="ph_close_cmd")
        self.phantom = phantom
        self.panda = panda
        self.rot_stage = rot_stage
        self.ph_open_cmd = ph_open_cmd
        self.ph_close_cmd = ph_close_cmd

        drv = phantom.driver
        set_mock_value(phantom.hdf.file_path_exists, True)
        set_mock_value(drv.max_frame_count, 100_000)

        state = {"post": 0, "end": 0, "armed": False}
        self._state = state

        callback_on_mock_put(
            drv.post_trig_frames,
            lambda value, **kw: state.__setitem__("post", int(value)),
        )
        callback_on_mock_put(
            drv.download_end_frame,
            lambda value, **kw: state.__setitem__("end", int(value)),
        )

        def fire_event():
            # The event trigger: post-trigger frames land, cine valid.
            set_mock_value(drv.trigger_received, 1)
            set_mock_value(drv.array_counter, state["post"])
            set_mock_value(drv.complete_and_valid, 1)

        def on_acquire(value, **kw):
            if value:
                state["armed"] = True
                set_mock_value(drv.waiting_for_trigger, 1)
            else:
                state["armed"] = False
                set_mock_value(drv.waiting_for_trigger, 0)
                set_mock_value(drv.trigger_received, 0)
                set_mock_value(drv.complete_and_valid, 0)

        def on_soft_trigger(value, **kw):
            if value and state["armed"]:
                fire_event()

        def on_sweep(value, **kw):
            # Motor mirror; a sweep while armed stands in for the PandA
            # train starting at the start angle (tomo path).
            set_mock_value(rot_stage.user_readback, value)
            if state["armed"]:
                fire_event()

        def on_capture(value, **kw):
            # The HDF plugin resets NumCaptured when capture starts.
            if value:
                set_mock_value(phantom.hdf.num_captured, 0)

        def on_download(value, **kw):
            if value:
                for count in range(1, state["end"] + 2):  # window 0..end
                    set_mock_value(drv.download_count, count)
                    set_mock_value(phantom.hdf.num_captured, count)

        callback_on_mock_put(drv.acquire, on_acquire)
        callback_on_mock_put(drv.send_software_trigger, on_soft_trigger)
        callback_on_mock_put(rot_stage.user_setpoint, on_sweep)
        callback_on_mock_put(phantom.hdf.capture, on_capture)
        callback_on_mock_put(drv.download, on_download)

        self.docs: list[tuple[str, dict]] = []
        self.RE.subscribe(lambda name, doc: self.docs.append((name, doc)))

    def reset(self):
        self.docs.clear()

    def events_by_stream(self) -> dict[str, int]:
        names = {
            doc["uid"]: doc["name"]
            for name, doc in self.docs if name == "descriptor"
        }
        return dict(Counter(
            names[doc["descriptor"]]
            for name, doc in self.docs if name == "event"
        ))

    def last_stop(self) -> dict:
        stops = [doc for name, doc in self.docs if name == "stop"]
        assert stops, "no stop document captured"
        return stops[-1]


def main() -> None:
    bl = PhantomMockBeamline()

    # -- take_images --------------------------------------------------------
    bl.reset()
    bl.RE(take_images(
        bl.phantom, ph_open_cmd=bl.ph_open_cmd, ph_close_cmd=bl.ph_close_cmd,
        output_dir=OUT, exposure_time=0.005, acquire_period=0.02,
        num_images=10,
    ))
    assert bl.last_stop()["exit_status"] == "success"
    assert bl.events_by_stream() == {"primary": 1}, bl.events_by_stream()
    print("PASS  take_images (1 primary event, 10-frame capture)")

    # -- dark_flat_scan -----------------------------------------------------
    bl.reset()
    bl.RE(dark_flat_scan(
        bl.phantom, bl.ph_open_cmd, bl.ph_close_cmd,
        output_dir=OUT, exposure_time=0.005, acquire_period=0.02,
        num_dark=3, num_flat=5,
    ))
    assert bl.last_stop()["exit_status"] == "success"
    assert bl.events_by_stream() == {"flat": 1, "dark": 1}, bl.events_by_stream()
    sres = [d for n, d in bl.docs if n == "stream_resource"]
    assert len(sres) == 2, f"expected flat+dark stream resources, got {len(sres)}"
    print("PASS  dark_flat_scan (flat + dark events, 2 stream resources)")

    # -- tomo_scan (structural: mock device tree + validation) --------------
    # The fly path (PandA PCOMP/PULSE1 + FSYNC + RAM download) needs the
    # design-specific panda blocks (calc) the mock HDFPanda doesn't have —
    # behavioral verification is the sim tier's job (the deep Phantom sim
    # tier), same split as tomo_flyscan in plans_mock_test.py.
    def start_plan(gen):
        """Drive the generator to its first message (plan bodies don't run
        until first next()), then shut it down."""
        msg = next(gen)
        try:
            gen.close()
        except RuntimeError:
            pass  # finalizer yields cleanup messages during close
        return msg

    msg = start_plan(tomo_scan(
        bl.phantom, bl.panda, bl.rot_stage,
        ph_open_cmd=bl.ph_open_cmd, ph_close_cmd=bl.ph_close_cmd,
        output_dir=OUT, exposure_time=0.005, num_projections=30,
        start_deg=0.0, stop_deg=30.0,
    ))
    assert msg is not None
    for bad_kwargs, expect in [
        (dict(num_projections=1), "num_projections"),
        (dict(num_projections=30, exposure_time=0.0), "exposure_time"),
        (dict(num_projections=30, exposure_time=0.005, acquire_period=0.5),
         "acquire_period"),
        (dict(num_projections=30, ph_open_cmd=None, ph_close_cmd=None),
         "use_shutter"),
    ]:
        kwargs = dict(
            output_dir=OUT, exposure_time=0.005,
            ph_open_cmd=bl.ph_open_cmd, ph_close_cmd=bl.ph_close_cmd,
        )
        kwargs.update(bad_kwargs)
        try:
            start_plan(tomo_scan(bl.phantom, bl.panda, bl.rot_stage, **kwargs))
        except ValueError as exc:
            assert expect in str(exc), exc
        else:
            raise AssertionError(f"expected ValueError for {bad_kwargs}")
    print("PASS  tomo_scan (structural: builds with mock panda; validation raises)")

    print("\nALL PHANTOM PLAN MOCK TESTS PASS")


if __name__ == "__main__":
    main()
