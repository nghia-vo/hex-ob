# Phantom vs Kinetix fly scans — and the trigger-window convention

Two things differ between the kinetix and phantom tomography fly paths.
Both come from the cameras' hardware, and one of them is a deliberate
convention change in the ported plans that the beamline team should review.

## 1. Where frames go during the scan

```
KINETIX (streaming camera)
PandA pulse ──▶ frame ──▶ plugin chain ──▶ HDF file
   pulse    ──▶ frame ──▶ plugin chain ──▶ HDF file    ← file grows DURING the sweep
   pulse    ──▶ frame ──▶ plugin chain ──▶ HDF file

PHANTOM (high-speed camera, onboard RAM)
PandA pulse ──▶ frame ──▶ camera's own RAM ┐
   pulse    ──▶ frame ──▶ camera's own RAM │           ← nothing touches disk yet
   pulse    ──▶ frame ──▶ camera's own RAM ┘
                     ...sweep ends...
              RAM ──(ethernet download)──▶ HDF file    ← file written AFTER the sweep
```

The Kinetix streams: every PandA pulse produces a frame that flows through
the plugin chain into the HDF file while the stage is still rotating.

The Phantom runs at up to ~10 kfps — faster than any network or disk — so
it records into its internal "cine" RAM during the scan and downloads that
memory to the HDF file afterwards. **Record fast, then download slow.**

This is why the plan ordering differs (`plans/phantom/tomo_scan.py` vs
`plans/tomography/tomo_flyscan.py`): the Phantom must be armed and
listening *before* the pulse train fires, so the plan arms the PandA
first, then kicks off the camera (under ophyd-async 0.19 kickoff is quick
bookkeeping — the wait for the event trigger lives in the device's
`complete()`), and lets the rotation sweep release it. The Kinetix plan
arms everything fully and then sweeps.

## 2. The "post-trigger download window"

The Phantom's RAM works like a **dashcam**: it records in a continuous
loop, overwriting the oldest frames. One special moment — the **event
trigger** — is "time zero", and every frame in RAM is numbered relative to
it: negative frames happened *before* the trigger, frame 0 onward *after*.
The **download window** is which slice of that loop gets pulled to disk.

```
LEGACY (dashcam style):        record...record...record ──TRIGGER── stop
                               └──────── save THESE ───────┘
                               frames -N..-1  (before the trigger)

PORTED (starting-gun style):   ──TRIGGER── frame frame frame ... stop
                                          └──── save THESE ────┘
                               frames 0..N-1  (after the trigger)
```

The legacy pyepics scripts (`hex-acq-pyepics/techniques/tomography/phantom/`)
let the camera record and fired the trigger *at the end*: "something worth
keeping just finished — save what's already in memory." The ported plans
(`plans/phantom/`) fire the trigger *first*: "go — the next N frames are
the ones I want." Either way exactly N frames land on disk; what changes is
whether the trigger marks the **end** or the **start** of the window.

**Why the ports flipped it:** the ophyd-async device (`lib/phantom.py`)
encodes one clean flow — arm, wait for the trigger, count N post-trigger
frames, download — and all phantom plans ride that single flow, which keeps
them uniform and testable. For tomography it is arguably more correct: the
PandA train starting at the start angle *is* the trigger, so frame 0 lines
up with the first projection. For dark/flat and take_images the legacy
trigger was only an "I'm done recording" marker, so no physics is lost —
but the beam-on window shifts relative to plan launch, which is exactly the
kind of change the beamline team should bless rather than discover.

## Review checklist (beamline session)

- [ ] Post-trigger window acceptable for dark/flat and take_images?
- [ ] Tomo: confirm the train start is wired as the camera's event trigger
      (TriggerEdge rising, ReadySignal RECORDING) on real hardware.
- [ ] One soft trigger per capture (legacy sent two, belt-and-braces).
