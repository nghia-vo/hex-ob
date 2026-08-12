"""
Shared Phantom camera setup stubs for the P-series plans.

Mirrors the "Prepare Phantom camera" block every legacy phantom script runs
(hex-acq-pyepics/techniques/tomography/phantom/*.py): stop anything left
running, wire the cine machinery to a known state, set the post-trigger
window, and take a black reference.  The acquisition itself is the device's
job (lib/phantom.py PhantomAcquireLogic): arm, wait for the event trigger,
count post-trigger frames, download RAM to the HDF chain.

Download-window convention (flagged for beamline review): these plans
download the POST-trigger window (frames 0 .. n-1 after the event), so the
event is sent FIRST and the frames of interest follow it.  The legacy
dark/flat and take_images scripts recorded first and triggered last,
downloading PRE-trigger frames from RAM — same frame count, different
timing convention, chosen so every plan rides the device's single
arm→trigger→count→download flow.
"""

import bluesky.plan_stubs as bps
from ophyd_async.core import OnOff

from lib.phantom import (
    PhantomExtSyncType,
    PhantomReadySignal,
    PhantomTrigEdge,
)

# Legacy per-script acquire-period ceilings (camera sync constraints).
MAX_ACQUIRE_PERIOD_TOMO = 1 / 50.0      # tomo_scan.py
MAX_ACQUIRE_PERIOD_INTERNAL = 1 / 24.0  # take_images.py / time_series_scan.py


def configure_phantom(
    phantom,
    *,
    post_trig_frames: int,
    acquire_period: float | None = None,
    auto_advance: bool = True,
):
    """
    Put the camera in the known state the legacy scripts establish, sized
    for *post_trig_frames* frames after the event trigger.

    Returns the effective frame count — clamped to the camera's
    MaxFrameCount like the legacy scripts (which print a warning and
    continue with the maximum).
    """
    drv = phantom.driver

    # Interrupted-scan recovery (legacy stop_acquire / close_hdf_stream /
    # stop_preview): nothing may be acquiring or capturing while we rewire.
    yield from bps.abs_set(drv.acquire, 0, wait=True)
    yield from bps.abs_set(phantom.hdf.capture, 0, wait=True)
    yield from bps.abs_set(drv.preview, 0, wait=True)

    max_frames = yield from bps.rd(drv.max_frame_count)
    if max_frames and post_trig_frames > max_frames:
        print(f"WARNING: requested {post_trig_frames} frames exceeds the "
              f"camera's capacity {max_frames} — clamped (legacy behavior).")
        post_trig_frames = max_frames

    yield from bps.mv(drv.partition_cines, 1)
    yield from bps.mv(drv.selected_cine, 0)  # legacy "cine 1" is index 0
    yield from bps.mv(drv.auto_save, OnOff.OFF)
    yield from bps.mv(
        drv.auto_advance, OnOff.ON if auto_advance else OnOff.OFF
    )
    yield from bps.mv(drv.auto_restart, OnOff.OFF)
    yield from bps.mv(drv.auto_bref, OnOff.OFF)
    yield from bps.mv(drv.trigger_edge, PhantomTrigEdge.RISING)
    yield from bps.mv(drv.ready_signal, PhantomReadySignal.RECORDING)

    if acquire_period is not None:
        yield from bps.mv(drv.acquire_period, acquire_period)

    yield from bps.mv(drv.post_trig_frames, post_trig_frames)
    # Post-trigger download window (see module docstring).
    yield from bps.mv(drv.download_start_frame, 0)
    yield from bps.mv(drv.download_end_frame, post_trig_frames - 1)

    return post_trig_frames


def black_reference(phantom, *, timeout_s: float = 60.0, poll_s: float = 0.2):
    """
    Take a black reference (legacy ``do_black_ref``): switch to FREE-RUN,
    trigger a CSR and poll until the camera reports it done (CSRCount 0).
    """
    drv = phantom.driver
    yield from bps.mv(drv.ext_sync_type, PhantomExtSyncType.FREE_RUN)
    yield from bps.abs_set(drv.perform_csr, 1, wait=True)
    for _ in range(max(1, int(timeout_s / poll_s))):
        count = yield from bps.rd(drv.csr_count)
        if count == 0:
            return
        yield from bps.sleep(poll_s)
    raise RuntimeError(
        f"Black reference (CSR) did not complete within {timeout_s} s"
    )


def wait_for_armed(phantom, *, timeout_s: float = 30.0, poll_s: float = 0.1):
    """
    Wait until the armed camera reports waiting_for_trigger — the moment it
    is safe to send/expect the event trigger.  (The device's kickoff runs
    the whole arm→trigger→download flow; plans that must interleave the
    event source — soft trigger, rotation sweep — poll this state bit.)
    """
    drv = phantom.driver
    for _ in range(max(1, int(timeout_s / poll_s))):
        waiting = yield from bps.rd(drv.waiting_for_trigger)
        if waiting == 1:
            return
        yield from bps.sleep(poll_s)
    raise RuntimeError(
        f"Phantom did not arm within {timeout_s} s "
        "(waiting_for_trigger never went 1)"
    )


def soft_triggered_capture(phantom, stream_name: str):
    """
    One complete soft-triggered cine capture into *stream_name*:
    kick off the (prepared) detector, wait for it to arm, send the software
    event trigger, wait for record+download to finish, emit the event.
    """
    group = f"phantom-{stream_name}"
    yield from bps.trigger(phantom, group=group, wait=False)
    yield from wait_for_armed(phantom)
    yield from bps.abs_set(phantom.driver.send_software_trigger, 1)
    yield from bps.wait(group=group)
    yield from bps.create(name=stream_name)
    yield from bps.read(phantom)
    yield from bps.save()
