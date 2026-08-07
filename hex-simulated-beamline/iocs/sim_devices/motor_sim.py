"""Simulated EPICS motor record(s) for the HEX sim.

Uses caproto's shipped ``fake_motor_record.FakeMotor`` — a *full* EPICS motor
record (all ~150 fields: VAL/RBV/VELO/ACCL/DMOV/MOVN/STOP/HLM/LLM/EGU/…) driven
by a simple simulator that ramps ``.RBV`` toward ``.VAL`` at ``.VELO`` and
toggles ``.DMOV`` 0→1 when done. That ``.DMOV`` transition is exactly what
ophyd-async ``Motor.set()`` waits on, and what the legacy pyepics
``RotationStage.wait_until_position`` polls — so this one record satisfies both
the hextools ``tomo_rot_axis`` and the pyepics reference client.

This module is used by the **dedicated** motor IOC (``iocs/motor/motor_ioc.py``),
which runs in its own process / event loop on its own CA port so the ~10 Hz
motion simulator is never starved by the detector + fabrication load in
``sim_ioc.py``. ``sim_ioc``'s blackhole excludes the motor namespace (see
:func:`record_exclude_prefix`) so it never answers the motor PVs — the dedicated
IOC does, on its own port.

NSLS-II PV names contain literal ``{ }``; caproto runs ``prefix.format()`` on
the PVGroup prefix, so we double the braces (``{{ }}``) to emit literal braces.

Zero/tiny-move fix: caproto's stock simulator flips ``.DMOV`` 0→1 within a single
event-loop tick for a zero-distance move (motor already at the setpoint), so a CA
monitor never observes the "moving" phase and ophyd v1's ``MoveStatus`` hangs
forever (its ``was_moving`` never becomes True). Tomography plans always move to
``start_deg`` first (often the current position), so we install a patched
simulator with a guaranteed one-tick moving phase (``num_steps >= 1``) via
:func:`patch_fake_motor`.
"""

import caproto.ioc_examples.fake_motor_record as _fmr
from caproto.ioc_examples.fake_motor_record import (
    FakeMotor,
    broadcast_precision_to_fields,
)

# Default simulated motors:
#   MC:5-Ax:4     rotation stage for tomography
#   SMPL:1-Ax:X1  sample tower X (flat-field in/out moves) — added 2026-08-07:
#                 the typed ophyd-async Motor demands enum/string record fields
#                 (.FOFF/.OUT/.SET) that the blackhole fabricates as floats, so
#                 sample_x must be a real FakeMotor record, not a blackhole PV.
DEFAULT_MOTOR_PVS = [
    "XF:27IDF-OP:1{MC:5-Ax:4}Mtr",
    "XF:27IDF-OP:1{SMPL:1-Ax:X1}Mtr",
]


async def _motor_simulator(instance, async_lib, defaults=None, tick_rate_hz=10.0):
    """caproto's motor_record_simulator with a guaranteed observable move phase."""
    if defaults is None:
        defaults = dict(
            velocity=0.1, precision=3, acceleration=1.0,
            resolution=1e-6, user_limits=(0.0, 100.0),
        )
    fields = instance.field_inst
    have_new_position = False
    # Put-completion state: real motor records hold CA put-completion until
    # the move finishes, and the pyepics scripts rely on it
    # (deco.RotationStage.move_to_position does caput(VAL, wait=True) and then
    # immediately reads the encoder — an instant completion made it read the
    # PREVIOUS position and program a wrong PCOMP start angle in the sim).
    # Sequence-token completion: each put gets a seq; the sim loop marks the
    # seq it started with as done when THAT ramp finishes. A retarget mid-move
    # bumps seq, so the newest put only completes after its own motion — a
    # single busy flag let a put be released by the PREVIOUS move's finish.
    move_state = {"target": None, "seq": 0, "done_seq": 0, "internal": False}

    async def value_write_hook(fields, value):
        nonlocal have_new_position
        if move_state["internal"]:
            return  # sim loop writing the value itself (STOP path)
        move_state["target"] = value
        move_state["seq"] += 1
        my_seq = move_state["seq"]
        have_new_position = True
        while move_state["done_seq"] < my_seq:
            await async_lib.library.sleep(0.02)

    fields.value_write_hook = value_write_hook

    await instance.write_metadata(precision=defaults["precision"])
    await broadcast_precision_to_fields(instance)
    await fields.velocity.write(defaults["velocity"])
    await fields.seconds_to_velocity.write(defaults["acceleration"])
    await fields.motor_step_size.write(defaults["resolution"])
    await fields.user_low_limit.write(defaults["user_limits"][0])
    await fields.user_high_limit.write(defaults["user_limits"][1])
    # Real motor records carry a .DESC; scripts use it as a metadata key
    # (deco.get_real_motor_name) and an empty one produced an empty HDF
    # dataset name, crashing losa.save_nxs_metadata.
    await fields.description.write(
        defaults.get("description", "HEX sim motor %s" % instance.pvname))

    while True:
        dwell = 1.0 / tick_rate_hz
        # The blocking hook stashes the target BEFORE caproto commits
        # instance.value, so prefer it while a put is being completed.
        seq_snapshot = move_state["seq"]
        target_pos = (move_state["target"] if move_state["target"] is not None
                      else instance.value)
        diff = target_pos - fields.user_readback_value.value
        total_time = abs(diff / fields.velocity.value)
        num_steps = int(total_time // dwell)
        if abs(diff) < 1e-9 and not have_new_position:
            if fields.stop.value != 0:
                await fields.stop.write(0)
            await async_lib.library.sleep(dwell)
            continue
        if fields.stop.value != 0:
            await fields.stop.write(0)
        # Guarantee at least one moving tick so a CA monitor sees DMOV 1->0->1
        # (fixes hung MoveStatus on zero/tiny-distance moves).
        num_steps = max(num_steps, 1)

        await fields.done_moving_to_value.write(0)
        await fields.motor_is_moving.write(1)

        readback = fields.user_readback_value.value
        step_size = diff / num_steps
        resolution = max((fields.motor_step_size.value, 1e-10))
        for _ in range(num_steps):
            if fields.stop.value != 0:
                await fields.stop.write(0)
                move_state["internal"] = True
                try:
                    await instance.write(readback)
                finally:
                    move_state["internal"] = False
                move_state["target"] = readback  # a stopped move stays put
                break
            if fields.stop_pause_move_go.value == "Stop":
                move_state["internal"] = True
                try:
                    await instance.write(readback)
                finally:
                    move_state["internal"] = False
                move_state["target"] = readback  # a stopped move stays put
                break
            readback += step_size
            await fields.user_readback_value.write(readback)
            await fields.dial_readback_value.write(readback)
            await fields.raw_readback_value.write(readback / resolution)
            await async_lib.library.sleep(dwell)
        else:
            await fields.user_readback_value.write(target_pos)

        await fields.motor_is_moving.write(0)
        await fields.done_moving_to_value.write(1)
        have_new_position = False
        # Release puts whose move this ramp covered; a retarget that arrived
        # mid-move bumped seq past the snapshot and stays pending. NEVER clear
        # the stashed target here: caproto commits instance.value only after
        # the blocked hook returns, so an iteration in that window would see
        # the stale old value and drive the motor back to it (observed as a
        # triple-move bounce).
        if move_state["done_seq"] < seq_snapshot:
            move_state["done_seq"] = seq_snapshot


def patch_fake_motor():
    """Make caproto's ``FakeMotor`` use the zero-move-safe simulator.

    ``FakeMotor``'s startup calls the module-level
    ``fake_motor_record.motor_record_simulator``; swapping it out makes every
    ``FakeMotor`` created afterwards use our patched version.
    """
    _fmr.motor_record_simulator = _motor_simulator


def motor_group(
    pv_name,
    *,
    velocity=30.0,
    acceleration=0.2,
    limits=(-360.0, 360.0),
    resolution=1e-6,
):
    """Return a caproto ``FakeMotor`` PVGroup serving a motor record at ``pv_name``.

    Merge ``.pvdb`` into a dedicated motor IOC and serve it (caproto resolves the
    record fields ``.VAL``/``.RBV``/… from the base channel). Braces in the
    NSLS-II PV name are doubled for caproto's ``str.format`` macro step.
    """
    escaped = pv_name.replace("{", "{{").replace("}", "}}")
    return FakeMotor(
        prefix=escaped,
        velocity=velocity,
        acceleration=acceleration,
        user_limits=limits,
        resolution=resolution,
    )


def record_exclude_prefix(pv_name):
    """blackhole exclusion prefix so ``sim_ioc`` never answers the motor record.

    ``pv_name`` is the base record (``…Mtr``); excluding this prefix keeps the
    blackhole silent for the record + its fields + ``Mtr*`` companions, so the
    dedicated motor IOC (own CA port) answers those searches without conflict.
    """
    return pv_name
