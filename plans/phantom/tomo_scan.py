"""
Phantom fly-scan tomography plan for HEX beamline.

Equivalent of the old pyepics script:
    hex-acq-pyepics/techniques/tomography/phantom/tomo_scan.py

The PandA runs the same PCOMP → PULSE1 train as the kinetix fly path, but
the Phantom consumes it differently: the pulses are the camera's FSYNC
frame clock, the train's start is the event trigger, the camera records
*num_projections* post-trigger frames into cine RAM, and the frames reach
the HDF writer during the RAM download after the sweep — all encoded in the
device's arm→trigger→count→download flow (lib/phantom.py).  PCAP still
captures the per-pulse ``Angle``.

Under ophyd-async 0.19 the camera's kickoff is quick bookkeeping — the
block-until-train lives in the device's acquire status, awaited by
``complete()`` — so both kickoffs wait. The ordering still differs from
the kinetix ``tomo_flyscan``: PandA first (arms PCOMP), then the camera's
kickoff, then the sweep — the train releases the camera as the stage
crosses the start angle, with ``complete()`` watching the flight.

Differences from the original (flagged for beamline review)
-----------------------------------------------------------
- The plan drives the rotation stage (velocity from the span/frame-period
  triangle, kinetix-style); the legacy script armed everything and asked
  the operator to start the rotation by hand.
- Post-trigger download window 0..n-1 (see plans/phantom/configure.py).
- No .nxs sidecar — metadata lives in the run documents.
- D-0012 note: this should eventually fold into the general ``tomo_fly``
  with device dispatch; the kickoff-ordering difference above is the piece
  the general plan must absorb first.

Usage
-----
    RE(tomo_scan(
        phantom1, panda1, rot_stage,
        ph_open_cmd=ph_open_cmd, ph_close_cmd=ph_close_cmd,
        output_dir=".../phantom/raw_data/scan_00001",
        exposure_time=0.005, num_projections=1801,
        start_deg=0, stop_deg=180,
    ))
"""

import bluesky.plan_stubs as bps
import bluesky.preprocessors as bpp
from ophyd_async.core import DetectorTrigger, TriggerInfo

from lib.detectors import set_output_dir
from lib.shutter import close_photon_shutter, open_photon_shutter
from plans.tomography.tomo_flyscan import (
    ENCODER_SETTLE_TIMEOUT,
    ENCODER_SETTLE_TOL,
    TOMO_ROTARY_STAGE_VELO_RESET_MAX,
    TOMO_ROTARY_STAGE_VELO_SCAN_MAX,
)

from .configure import (
    MAX_ACQUIRE_PERIOD_TOMO,
    black_reference,
    configure_phantom,
    wait_for_armed,
)

OVERHEAD = 2e-6  # s, legacy phantom frame-period overhead


def tomo_scan(
    phantom,
    panda,
    rot_stage,
    *,
    output_dir: str,
    exposure_time: float,
    num_projections: int,
    start_deg: float = 0.0,
    stop_deg: float = 180.0,
    lead_angle: float = 10.0,
    acquire_period: float = 0.0,
    reset_speed: float = TOMO_ROTARY_STAGE_VELO_RESET_MAX,
    use_shutter: bool = True,
    ph_open_cmd=None,
    ph_close_cmd=None,
    fe_shutter_status=None,
    md: dict | None = None,
):
    """
    Phantom fly-scan tomography: rotate continuously, PandA FSYNC-clocked
    frames into cine RAM, RAM download after the sweep.

    Parameters mirror :func:`plans.tomography.tomo_flyscan` (the camera is
    a ``PhantomDetector`` from ``lib.phantom.make_phantom``); the phantom's
    frame period must stay within the camera's sync ceiling (legacy 1/50 s).
    """
    if stop_deg <= start_deg:
        raise ValueError(
            f"stop_deg ({stop_deg}) must be > start_deg ({start_deg})"
        )
    if num_projections < 2:
        raise ValueError(f"num_projections must be >= 2, got {num_projections}")
    if exposure_time <= 0:
        raise ValueError(f"exposure_time must be > 0, got {exposure_time}")
    if use_shutter and (ph_open_cmd is None or ph_close_cmd is None):
        raise ValueError(
            "use_shutter=True needs ph_open_cmd and ph_close_cmd "
            "(or pass use_shutter=False)."
        )

    if (acquire_period + OVERHEAD) < exposure_time or acquire_period == 0.0:
        acquire_period = exposure_time + OVERHEAD
    if acquire_period > MAX_ACQUIRE_PERIOD_TOMO:
        raise ValueError(
            f"acquire_period {acquire_period:.6g} exceeds the camera's "
            f"maximum {MAX_ACQUIRE_PERIOD_TOMO:.6g} s (legacy 1/50 rate floor)"
        )

    reset_speed = min(reset_speed, TOMO_ROTARY_STAGE_VELO_RESET_MAX)

    output_path = set_output_dir(phantom, output_dir, "proj")
    set_output_dir(panda, output_dir, "panda")

    _md = {
        "plan_name": "phantom_tomo_scan",
        "detectors": [phantom.name],
        "motors": [rot_stage.name],
        "output_dir": str(output_path),
        "exposure_time": exposure_time,
        "num_projections": num_projections,
        "start_deg": start_deg,
        "stop_deg": stop_deg,
        "lead_angle": lead_angle,
        "hints": {},
    }
    _md.update(md or {})

    panda_pcomp = panda.pcomp[1]
    panda_pulser = panda.pulse[1]

    staged: dict = {"devices": None}

    def _wait_encoder_settled(label):
        # Same calibration-free settle as tomo_flyscan: two reads agree.
        last = yield from bps.rd(panda.calc[2].out)
        for _ in range(int(ENCODER_SETTLE_TIMEOUT / 0.2)):
            yield from bps.sleep(0.2)
            now = yield from bps.rd(panda.calc[2].out)
            if abs(now - last) < ENCODER_SETTLE_TOL:
                return now
            last = now
        raise RuntimeError(
            f"Encoder still moving at the {label} position "
            f"(last read {last:.0f} counts)"
        )

    def _scan():
        if use_shutter:
            if fe_shutter_status is not None:
                if (yield from bps.rd(fe_shutter_status)) != 1:
                    raise RuntimeError("Front-end shutter is closed. Reopen it!")
            yield from open_photon_shutter(ph_open_cmd)

        # Frame period / sweep velocity triangle (kinetix recompute rules;
        # the phantom has no readout-mode rate signal — its cap is the
        # acquire_period ceiling validated above).
        step_time = acquire_period
        scan_time = (num_projections - 1) * step_time
        rot_motor_vel = abs(stop_deg - start_deg) / scan_time
        if rot_motor_vel > TOMO_ROTARY_STAGE_VELO_SCAN_MAX:
            rot_motor_vel = TOMO_ROTARY_STAGE_VELO_SCAN_MAX
            scan_time = abs(stop_deg - start_deg) / rot_motor_vel
            step_time = scan_time / (num_projections - 1)
        if exposure_time > step_time:
            raise RuntimeError(
                f"Exposure time {exposure_time} s is longer than the final "
                f"time per step {step_time:.6g} s (after the velocity cap)!"
            )

        # Camera state + black reference (FREE-RUN), before FSYNC arming.
        effective = yield from configure_phantom(
            phantom, post_trig_frames=num_projections,
            acquire_period=step_time,
        )
        if effective != num_projections:
            raise RuntimeError(
                f"Camera RAM holds only {effective} frames — reduce "
                f"num_projections ({num_projections})."
            )
        yield from black_reference(phantom)

        # -- position the stage and read the start-angle encoder count ----
        yield from bps.mv(rot_stage.velocity, reset_speed)
        yield from bps.mv(rot_stage, start_deg)
        start_encoder = yield from _wait_encoder_settled("start")
        yield from bps.mv(rot_stage, start_deg - lead_angle)
        yield from _wait_encoder_settled("lead")
        yield from bps.mv(rot_stage.velocity, rot_motor_vel)

        # -- PandA: PCOMP fires once at the start angle, PULSE1 emits the
        # FSYNC train (time-trigger mode, like the kinetix path) ----------
        yield from bps.mv(panda_pcomp.start, int(start_encoder))
        yield from bps.mv(panda_pcomp.width, 3)
        yield from bps.mv(
            panda_pcomp.pulses, 1,
            panda_pulser.pulses, num_projections,
            panda_pulser.step, step_time,
            panda_pulser.width, exposure_time / 5,
        )
        yield from bps.mv(panda.calc[2].out_dataset, "Angle")

        yield from bps.open_run(md=_md)
        print(f"\nExecuting Phantom tomography scan: {num_projections} "
              f"projections, {start_deg:g} -> {stop_deg:g} deg "
              f"at {rot_motor_vel:.3g} deg/s")

        all_devices = [panda, phantom]
        yield from bps.stage_all(*all_devices)
        staged["devices"] = all_devices

        yield from bps.prepare(
            phantom,
            TriggerInfo(
                trigger=DetectorTrigger.EXTERNAL_EDGE,  # -> FSYNC
                livetime=exposure_time,
                deadtime=step_time - exposure_time,
                number_of_events=1,
                collections_per_event=num_projections,
            ),
            wait=True,
        )
        yield from bps.prepare(
            panda,
            TriggerInfo(
                number_of_events=num_projections,
                trigger=DetectorTrigger.EXTERNAL_LEVEL,
                livetime=step_time,
                deadtime=0.0001,
            ),
            wait=True,
        )

        yield from bps.declare_stream(*all_devices, name="tomo")

        # PandA first (arms PCOMP at the start angle), then the camera.
        # Under ophyd-async 0.19 the camera's kickoff is quick bookkeeping
        # (the block-until-train lives in the device's acquire status,
        # awaited by complete), so kickoff waits here — complete() needs
        # its context in place before collect_while_completing runs.
        yield from bps.kickoff(panda, wait=True)
        yield from bps.kickoff(phantom, wait=True)
        yield from wait_for_armed(phantom)

        yield from bps.abs_set(rot_stage, stop_deg + lead_angle,
                               group="tomo_sweep")

        yield from bps.collect_while_completing(
            all_devices, all_devices,
            flush_period=max(1, exposure_time),
            stream_name="tomo",
        )
        yield from bps.unstage_all(*all_devices)
        staged["devices"] = None

        yield from bps.wait(group="tomo_sweep")
        yield from bps.close_run()

        captured = {panda.name: (yield from bps.rd(panda.data.num_captured)),
                    phantom.name: (yield from bps.rd(phantom.hdf.num_captured))}
        print("Number of frames captured:")
        for name, count in captured.items():
            print(f"    {name:15}: {count}")

    def _cleanup():
        if staged["devices"] is not None:
            yield from bps.unstage_all(*staged["devices"])
            staged["devices"] = None
        if use_shutter and ph_close_cmd is not None:
            yield from close_photon_shutter(ph_close_cmd)
        yield from bps.mv(rot_stage.velocity, reset_speed)

    return (yield from bpp.finalize_wrapper(_scan(), _cleanup()))
