# Simulated Phantom — real ADPhantom IOC + fake camera (scaffold)

The Phantom tier of the simulated HEX beamline, per the accepted
architecture decision (2026-08-11): run the **real deployed ADPhantom
module** with the camera faked at the **wire-protocol level** — the same
pattern as the PandA tier (real engine + protocol sim), and the only shape
that honestly serves the digital-twin goal: all 8,328 live records and the
unmodified ADPhantom Phoebus screens work against the sim.

Two pieces:

- **`sim_camera.py`** — the fake camera: a loopback TCP pair (CTRL `:7115`,
  DATA `:7116`) speaking the PH16 command surface the deployed driver uses.
  Python-3 port of Diamond's `sim/SimServer.py` (shipped unmodified in the
  deployed module), extended with the fork's surface: `set`, dotted-path
  gets, `defc`/`auto`/`irig`/`meta` structs, 63 cine structs, the
  WTR→TRG→ACT→STR cine state machine, and `img {cine,start,cnt,fmt}`
  downloads. Reply framing matches the *fork's* parser (`Ok!` / `ERR:`,
  newline-terminated — the ancestor sim's bare `OK` would not work).
  Protocol-level tests: `tests/phantom_simcam_mock_test.py` (runs in the
  mock-tier CI).
- **The IOC** (BUILT 2026-08-11) — the deployed ADPhantom module run via
  the `nsls2.ioc_deploy` adphantom role against
  [`hexsim-phantom1.yml`](hexsim-phantom1.yml) (the live `phantom-det1.yml`
  with `CAMERA_IP: 127.0.0.1`), following the kinetix build path:

  ```bash
  # 1. role deploy into the el8 container (compiles ADCore + ADPhantom)
  cd ~/git_projects/nsls2.ioc_deploy
  pixi run deployment --container -c .../iocs/phantom/hexsim-phantom1.yml
  # 2. carry over the BEAMLINE'S INSTALLED camera template — the live
  #    db/phantomCamera.template on xf27id1-det1 is hand-edited (adds the
  #    AcquireTimeMs ms-helper records, absent from git; the ophyd device
  #    NEEDS them). db/ is gitignored upstream, so the role can never
  #    install this — it must be copied from the rsynced deployed module:
  docker cp ~/git_projects/ADPhantom-deployed/db/phantomCamera.template \
      nsls2_ioc_deploy_el8:/epics/modules/adphantom_afefafc/db/phantomCamera.template
  # 3. snapshot as the compose image
  docker commit nsls2_ioc_deploy_el8 hexsim-phantom-ioc:local
  ```

  CA bound loopback-only on the dedicated port **:5105** (kinetix :5085,
  panda-ioc :5095, motor :5075). `scripts/up_all.sh` starts `sim_camera`
  first (the driver opens its sockets at iocInit), then the compose
  service; `scripts/env.sh` lists :5105.

## Bring-up results (2026-08-11)

- IOC boots to `completed startup`; driver attaches to `sim_camera`
  (`attachToPort response: Ok!`, connection status 0) and serves **8371
  records vs the beamline's 8328** — the delta is entirely infrastructure
  (iocStats naming variants, Codec LZ4/Zlib params, autosave enums from
  the module-version skew); the `cam1:` surface matches exactly after the
  template carry-over above.
- The full ophyd-async `PhantomIO` device **connects end-to-end** against
  the live sim IOC (every PvSuffix resolves; identity/exposure/cine reads
  correct through driver → PH16 protocol → sim).
- **First genuine sim-tier catch** (invisible to the mock tier, which
  fabricates matching enums): the device declared `Aux3PinMode` (no such
  record on the real IOC — pins are 1/2/4) and paired setpoint/readback
  as `rw_rbv` although the real mbbo has 12 choices vs the mbbi's 16
  (`CEVENT/CMEMGATE/CFSYNC/CPRETRIG`) — connect *at the beamline* would
  have failed identically. Fixed in `lib/phantom.py` + the hextools copy
  (pins 1/2/4; separate setpoint enum + str readback).

## Ground truth this scaffold is built on (no guessed semantics)

| Fact | Source |
|---|---|
| Reply framing `Ok!` / `ERR:`, `\n`-terminated both ways | `ADPhantom.h:58-59`, `ADPhantom.cpp:1234` (deployed source, rsynced from xf27id1-det1) |
| Command verbs + struct/param paths | grep of `ADPhantom.cpp` (`get/set/rec/trig/attach/img/ximg/time/setrtc/rel/del`) |
| Struct reply format (tab dict, `\`-CRLF continuations) | ancestor `sim/SimServer.py` (byte-identical in the deployed module) |
| `ArrayCounter_RBV = lastfr+1` = post-trigger count during recording | `ADPhantom.cpp` status poll (~line 1370) |
| Cine state tokens WTR/TRG/ACT/STR | `ADPhantom.cpp` `checkState` calls |
| `NUM_CINES: 63`, prefix, camera IP/ports | live `phantom-det1.yml` + `records.dbl` snapshot |
| Role pin `adphantom_afefafc` ≡ beamline `adphantom_329598e` + one `.gitignore` line — driver code byte-identical | `git diff --stat 329598e afefafc` in the ADPhantom clone (verified 2026-08-11) |

## Honest TODOs (marked in `sim_camera.py`)

- **TODO(format)** — dotted-path *get* reply framing not yet verified
  against the driver's parser (struct gets are — the driver's observed
  traffic uses struct gets only).
- **TODO(timing)** — `trig` completes the post-trigger phase instantly;
  real cameras pace at the programmed rate. (Downloads DO pace at 1G wire
  speed now — see below.)

## Download-path findings (2026-08-12, driven out by the live suite)

Chasing `take_images` end-to-end surfaced four defects — none visible to
the mock tier:

1. **Flag lists must be UNQUOTED** (`state : { WTR ACT },`): the driver's
   `parseDataStruc` files a flag-list item only through its
   repeat-terminator special case, which quoting defeats — with quotes,
   `c<n>.state` silently never reaches `paramMap_` and the State records
   stay 0. The ancestor Diamond sim quotes them too and carries the same
   latent defect.
2. **`irig.yearbegin` is load-bearing**: `readoutDataStream` integer-parses
   it before the first `img` request; a missing key silently aborts every
   download (the ancestor sim has no `irig` struct at all — it predates
   the fork's usage).
3. **`time {cine,start,cnt}` must stream 12 bytes/frame** on the data port
   BEFORE `img` is ever requested; an `Ok!` with no stream wedges the
   driver's download thread (an IOC restart is the only recovery).
4. **Per-frame `img` size is the driver's contract, not the cine's
   `frsize`**: `width*height*bits/8`, bits from the `fmt` token
   (P10→10, P12L→12, 8/8R→8, P16→16). Zero bytes are valid pixels in
   every packing, so correctly-sized zero-frames satisfy the full
   parse→convert→NDArray→HDF path (h5py-verified). Downloads pace at 1G
   wire speed (~10 ms/1.3 MB frame) — instant delivery is unphysical AND
   coalesces the driver's per-frame counter updates into nothing.

Device-side catch (fixed in `lib/phantom.py` + hextools): triggering the
RAM download in one coroutine and watching `DownloadCount` from another
is a race — a fast download completes and the driver resets the counter
to 0 before the watcher subscribes. The watch now subscribes, THEN
triggers, in one task, and treats reset-after-progress as completion.

## tomo_scan bring-up findings (2026-08-12, sim catch #3)

Running `tomo_scan` behaviorally (real phantom + rot_stage, mock PandA per
`dec:phantom-suite-mock-panda-interim`) surfaced an EXTERNAL-trigger
deadlock chain the soft-trigger plans could never hit:

1. **Busy-record deadlock**: the device's arm called
   `set_and_wait_for_other_value(acquire, True, ...)` without
   `wait_for_set_completion=False`. `Acquire` is a busy record — its
   put-completion fires when acquisition ENDS — so the arm blocked on the
   very trigger it was arming for. Soft-trigger plans masked it because
   `bps.trigger(wait=False)` hides the block inside a status while the
   plan fires the trigger itself; `prepare(wait=True)` (which arms
   external-trigger detectors in 0.19) deadlocked outright.
2. **0.19 acquire-logic contract**: `start_acquiring()` must RETURN once
   armed, stashing the trigger-wait → count → download flow in
   `acquire_status` (base-class shape) — ours ran the whole flow inline.
3. **Stale kickoff lore in the plan**: `tomo_scan` kicked the camera off
   `wait=False` ("kickoff blocks until the train fires" — 0.17 semantics).
   In 0.19 kickoff is quick bookkeeping and `complete()` needs its context
   in place, so `wait=False` raced into `RuntimeError('Kickoff not
   called')`. Now `wait=True`.

With those fixed, the interim suite is fully green: take_images (10),
dark_flat_scan (5+3) and tomo_scan (61 projections, sweep + train
stand-in) all write h5py-verified HDF through the real driver. The PandA
Angle series stays deferred to the full gate (design capture pending).

## Open beamline-side questions (AJ recon)

- **Trigger wiring**: which PandA output reaches the Phantom's FSYNC input
  vs its event-trigger input, and whether the train start is the event
  trigger on real hardware (assumed by `plans/phantom/tomo_scan.py`).
- **Time-series PandA design**: the legacy helpers drive `PULSE2` (scan
  train) gated by `BITS:A` — capture that design's block wiring from the
  PandA GUI (the way `Tomo_radio_1` was captured for the kinetix tier), so
  the sim design and `time_series_scan` can be built against it.

## Run (camera sim only, until the IOC lands)

```bash
hex-simulated-beamline/.toolenv/bin/python \
    hex-simulated-beamline/iocs/phantom/sim_camera.py   # 7115/7116 loopback
```
