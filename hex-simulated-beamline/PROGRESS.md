# HEX sim — running progress log

> **Moved 2026-08-07:** this simulated beamline's canonical home is now
> `hex-ob/hex-simulated-beamline/` (was `hxm_program/simulated_beamlines/HEX`,
> its temporary genesis location) — see hxm_program `DECISIONS.md` and the
> design graph (`dec:sim-home-hex-ob`). Planning/task tracking stays in
> `hxm_program`; this file remains the sim's own running log.

Current goal: make the simulated PandA actually **capture** during a tomography
fly scan (so `PCAP:ACTIVE` latches on arm and `NUM_CAPTURED` / the `Angle`
dataset flow), then run the pyepics tomography scripts against the sim.

## ✅ Building the simulated beamline (how we got here)

Governed by [`../../DECISIONS.md`](../../DECISIONS.md) **D-0014**: the sim is
grown one **minimal increment per HXM-1288 step**, never built out up front;
the pyepics scripts are the acceptance oracle. Interim home in this docs repo
per **I-015**; full brief in
[`../../planning/tasks/hex/sim-hex-beamline.md`](../../planning/tasks/hex/sim-hex-beamline.md).

- **Phase 1 — services + blackhole (✅ 2026-07-24).** `compose/docker-compose.yml`
  stands up the off-beamline service deps on 127.0.0.1 only: **Redis 7 (TLS
  :6380**, plaintext disabled, matching new NSLS-II redis machines), **Mongo
  4.4, Kafka 3.9 KRaft, Tiled** (ephemeral catalog, seeded `hex/raw`);
  `scripts/up.sh` generates the cert, waits for health, seeds. The vendored
  **blackhole IOC** (`iocs/blackhole/spoof_beamline.py`, from
  NSLS2/test-beamline-profiles) fabricates any incidental PV over CA, with a
  HEX addition: `BLACKHOLE_EXCLUDE_PREFIXES` so typed sims aren't shadowed.
- **PandA sim, route "b" — real PVA, no mocking (✅ 2026-07-24, upgraded
  2026-07-27).** `iocs/panda/` builds PandABlocks-server (pinned 4.1) driven by
  the **PandABlocks-FPGA block-sim engine** ("Option C") with a real
  `pandabox-no-fmc` app config, plus the published `pandablocks-ioc` 0.11.5
  serving CA+PVA for `XF:27ID1-ES{PANDA:1}` — so `HDFPanda` introspects a live
  `:PVI` and connects for real. Details in section 1 below and
  [`iocs/panda/README.md`](iocs/panda/README.md).
- **Full profile boot against the sim (✅ verified 2026-07-24).** With a
  `HEX_SIM=1` gate (branch `sim-boot`, lands via PR per D-0013), all 15
  `hex-profile-collection/startup/*.py` run to **BOOT COMPLETE**: `panda1`
  connects over PVA, RunEngine on the local TLS Redis, `TiledWriter` on local
  Tiled — devices connect for real, not ophyd-mocked.
- **Phase 2B — typed per-detector sims (✅ 2026-07-27).**
  `iocs/sim_devices/_ophyd_async_sim.py` introspects a mock-connected
  ophyd-async device and emits a correctly-typed caproto channel per signal
  (bool→enum, StrictEnum members, int/float/str) — no hand-maintained PV
  lists. `kinetix_sim.py` covers both HEX Kinetix cameras; `iocs/sim_ioc.py`
  composes typed sims + blackhole fallback into **one** CA server (:5064; two
  caproto servers on one host go deaf on UDP 5064 searches). Verified:
  `kinetix1`/`kinetix3` connect with typed `TriggerMode`/`ReadoutPortIdx`.
- **Dedicated motor IOC (✅ 2026-07-28, HXM-1288 flyscan work).**
  `iocs/motor/motor_ioc.py` runs caproto `FakeMotor` record(s) on their own
  process + CA port (:5075) so the ~10 Hz motion sim is never starved by the
  detector/fabrication load; DMOV cycles even on zero-distance moves.
- **Design under reflow2 control (2026-07-28).** The sim was adopted into the
  project's design graph: components/interfaces/capabilities recovered from
  the artifacts, requirements traced to the planning docs, verifications
  recorded with real pass/fail status.

## ✅ Done & verified — PandA fly-scan capture (the current increment)

### 1. Option C — engine-backed PandA sim (`PCAP:ACTIVE` latches on arm)
Root cause of the old "nothing ticks": the `panda-sim` container ran the
PandABlocks-server bundled `python/sim_server` (register echo, **no block logic**).
Now it runs the real **PandABlocks-FPGA block-sim engine**.
- `iocs/panda/fpga_sim_server.py` — py3 wrapper around
  `common/python/simulations.py` `SimulationController`; reimplements the register
  socket loop in py3 (shipped autogen sim server is py2). Selected via `simserver -f`.
  - drops FPGA-only `sfp_panda_sync` blocks (no soft-build pin metadata).
  - `configure_pcap_registers()` re-derives the `*REG` PCAP offsets from the
    generated `registers` file (this app puts `ARM` at 13, not the engine's
    hardcoded 10) and maps them to the bare names `pcap_sim` acts on. **Without
    this, `*PCAP.ARM=` is silently dropped and ACTIVE never latches.**
  - `BlockSimulation.__lt__ = id`-order — fixes an engine `bisect` crash when two
    wakeups share a timestamp (happens as soon as a position fans out to >1 listener).
- `iocs/panda/Dockerfile.simserver` — ships the FPGA tree at `/fpga`
  (`PANDA_FPGA_ROOT`), runs `simserver -n -f /panda`.
- VERIFIED: `PCAP:ACTIVE` reads `1` on `*PCAP.ARM=` over the **control protocol
  AND PVA** (pandablocks-ioc) — the exact gate ophyd-async's arm blocks on.

### 2. Motor → INENC position bridge (capture chain)
The sim motor (caproto FakeMotor IOC :5075) has no electrical link to the PandA,
so PandA never saw motion → no `PCOMP` → no capture. Closed in software:
- `fpga_sim_server.py` — **position-injection channel** on `127.0.0.1:9101`
  (env `PANDA_INJECT_PORT`). Line protocol `"<BLOCK> <FIELD> <int>"`, e.g.
  `INENC1 VAL 1600`; applies it as `do_tick({inst:{FIELD:value}})` so the engine
  fans it out to CALC/PCOMP listeners.
- `iocs/panda/hex_tomo_design.py` — wires the tomo design over the control
  protocol (bare `pandabox-no-fmc` has all muxes ZERO):
  `INENC1.VAL → CALC2 (identity, = get_enc_value) & PCOMP1.INP`;
  `PCOMP1.OUT → PULSE1.TRIG → PULSE1.OUT → PCAP.TRIG`; `BITS.A` = software run gate
  enabling PCOMP1/PULSE1/PCAP; capture `CALC2.OUT` (the Angle).
- `iocs/panda/motor_encoder_bridge.py` — host script: camonitors the motor `RBV`
  over CA, converts to encoder counts (`deg*counts_per_deg + offset`, defaults
  200 / −39660 per pyepics `lib_device_control`), injects into `INENC1.VAL`.

### 3. Live-motor capture (2026-07-28) — the "0 frames on a real move" bug, fixed
Three compounding sim-side defects (the acquisition scripts were never at fault):
1. **Engine dropped pending wakeups.** `do_tick`/`process_blocks` discarded a
   block's queued wakeup whenever a new change arrived for it — so PCOMP/PULSE/
   PCAP holding a queued `ENABLE=1` (from `BITS.A=1`) lost it as soon as a
   position injection fanned out `INP`/`POS_BUS`. With the bridge streaming,
   deterministic 0-capture (`BITS.A=1` but `BITS.OUTA=0`). Fixed in
   `fpga_sim_server.py` by replacing `do_tick`/`process_blocks` with versions
   that **merge** colliding changes (and a safe pending-wakeup pop — the stock
   `remove_wakeup` bisect also TypeErrors on ts collisions).
2. **Injection replies could wedge the whole server.** `_handle_inject` used a
   blocking `sendall` per `OK` reply; a client that never read (the old bridge)
   filled the socket buffer after ~40 min and froze the engine loop. Replies are
   now best-effort on a non-blocking socket, and the bridge drains them.
3. **Register writes lost the sign.** The C server transports writes as raw
   uint32; fields the app ini declares **signed** (`param int`: PCOMP
   START/STEP/WIDTH/PRE_START…) written negative (real encoder offset −39660!)
   became ~4×10⁹ thresholds `posn() >= next_crossing` could never reach —
   PCOMP sat in WAIT_RISING forever. `install_signed_writes()` sign-extends
   exactly the ini-declared-signed fields. (The old positive-range test masked
   this; any test with the real offset hit it.)
- `motor_encoder_bridge.py` now **slew-limits** the injected position
  (`--max-step` counts per `--rate-hz` tick, default 50 @ 50 Hz) so a coarse
  FakeMotor RBV step can never hop PCOMP past a compare point, and only sends
  on change.
- VERIFIED: `tests/slowmove_capture_test.py` **PASS 4/4** (5/5 frames at the
  real −39660 offset, VELO 2 and 4 deg/s); `tests/inj_capture_test.py` PASS.

### 4. `NUM_CAPTURED` / `Angle` dataset over the IOC (2026-07-28)
The full pandablocks-ioc data path works: `CALC2:OUT:DATASET=Angle`, `DATA:*`
configured, `DATA:Capture=1`, `PCAP:ARM=1`, live motor move →
**`DATA:NumCaptured = 5`** and `/tmp/hex-sim-data/panda.hdf` contains an
**`Angle` dataset** with the 5 captured encoder values (what deco reads via
`losa.load_hdf(panda.hdf, "Angle")`).
- VERIFIED: `tests/ioc_hdf_capture_test.py` PASS (drives everything through the
  published IOC PVs — including signed `PCOMP1:START` — no raw control access).

### Repo tests (were /tmp scripts — now permanent)
`iocs/panda/tests/`: `inj_capture_test.py` (engine chain, injection only —
stop the bridge first), `slowmove_capture_test.py` (motor → bridge → capture),
`ioc_hdf_capture_test.py` (IOC PVs → HDF `Angle`). All exit 0 on PASS and are
offset-agnostic (window computed relative to `CALC2:OUT` baseline).

### 5. Real PandA design recovered AND the sim rewired to it (✅ 2026-07-30)
AJ captured the live block panels of the production design
**`Tomo_radio_1_config`** (all priorities); full values + mechanism in
[`iocs/panda/designs/tomo_radio_1_config.md`](iocs/panda/designs/tomo_radio_1_config.md).
The real chain: **arming is the gate** (`PCOMP1.ENABLE = PCAP.ACTIVE`; BITS.A
belongs to the radiography path PULSE3→PULSE2), "time mode" is a
**position-armed time train** (PCOMP1 `PULSES=1` fires once at the start angle
→ PULSE1's N-pulse train → `PCAP.GATE/TRIG` and `LUT1=A|B` → `TTLOUT1` →
camera), CALC1 **negates** the backwards raw encoder (raw = 39660 − 200·deg),
and CALC2 is captured with **scale 0.005 / offset 198.3 → HDF "Angle" in
degrees**. `hex_tomo_design.py` now applies exactly this; the bridge default
became deg×−200 + 39660; one more engine defect found+fixed (`pulse_sim`
never returns `edge_ts` as a wakeup, so pulse trains died after the first
edge in the event-driven server).
- VERIFIED: all three `tests/` PASS against the real design (4/4 live-move
  runs incl. 4 deg/s), and the IOC path writes `panda.hdf` with **`Angle` in
  real degrees** ([1.025 … 4.105] for a 2 deg/s move sampled at 0.4 s).

### 6. Beamline guard + coverage checks (✅ 2026-07-30)
Per AJ's decision (sim run from a HEX-subnet host must never address real
devices): `iocs/panda/localguard.py` forces `EPICS_CA_AUTO_ADDR_LIST=NO` /
`EPICS_PVA_AUTO_ADDR_LIST=NO` and refuses to start any sim-side EPICS client
(bridge, capture tests) unless every CA/PVA address is loopback — the sim
reuses REAL PV names, so a beamline network must be unreachable by
construction. Also added the two missing coverage checks.
- VERIFIED: `tests/localguard_test.py` (5 refusal/pass cases incl. the real
  bridge entry point), `tests/motor_motion_test.py` (RBV ramp + DMOV cycle),
  `tests/blackhole_fabrication_test.py` (self-contained spoof on :5099;
  note caproto servers key off `EPICS_CA_SERVER_PORT` — set both). Capture
  tests re-PASS with the guard active.

## 🚧 Remaining / next session

**Run `tomo_flyscan.py` (pyepics) against the sim (HXM-1288 "before" leg).**
One gap left (was two — the design gap closed above):
✅ **Kinetix frames (Phase 2A) — DONE (2026-07-30).** Built the
   facility-native way: `nsls2.ioc_deploy` **adsimdetector role** deployed
   `--container` (epics-alma8) with the real HEX prefix
   ([`iocs/kinetix/hexsim-kinetix1.yml`](iocs/kinetix/hexsim-kinetix1.yml)),
   container committed to `hexsim-kinetix-ioc:local`, run via
   [`compose/docker-compose.kinetix.yml`](compose/docker-compose.kinetix.yml)
   — host network, **dedicated CA :5085** (a second EPICS server on :5064
   beside panda-ioc goes deaf to unicast searches), **loopback-only binds**
   (safety guardrail), `/tmp/hex-sim-data` mounted. `scripts/env.sh` now
   lists :5085. After any container recreate run
   [`iocs/kinetix/init_kinetix.py`](iocs/kinetix/init_kinetix.py) — the
   autosave-equivalents the scripts rely on but never set
   (`cam1:ArrayCallbacks=1`; without it the driver makes frames but plugins
   receive none).
   - VERIFIED: `tests/kinetix_frames_test.py` PASS — deco-style HDF sequence
     → `HDF1:NumCaptured_RBV=5` and a real, h5py-readable
     `/entry/data/data (5,1024,1024)` file, written+closed by the production
     Stream-mode plugin.
   - Fidelity notes: `ReadoutPortIdx`/`ApplyReadoutMode` absent (unused by
     tomo_flyscan.py); camera free-runs at `AcquirePeriod` instead of
     following the PandA TTLOUT1 trigger. Gotchas logged: stale
     `nsls2_ioc_deploy_el8` container had broken DNS (remove, redeploy).

✅ **Sim data-session + storage (2026-07-30, corrected approach).** Never
   create a real-looking `/nsls2` on a host or use a real proposal number —
   `/nsls2/data` is centrally-mounted facility storage and proposal numbers
   belong to real customers (AJ). Instead the real process is mimicked:
   [`scripts/sync_sim_experiment.sh`](scripts/sync_sim_experiment.sh) mirrors
   `nslsii.sync_experiment.switch_redis_proposal` — same Redis keys/shapes
   (`data_session`, `username`, `cycle`, `tiled_access_tags`,
   `proposal{...}`) with an unmistakably fake identity (sentinel
   **`pass-000000`**, proposal `type: SIMULATED`; LDAP/PASS steps skipped by
   design) — and provisions the storage mimic UNDER THE SIM ROOT ONLY:
   `/tmp/hex-sim-data/nsls2/data/hex/proposals/<cycle>/pass-000000/…` with a
   `_SIMULATED_DATA_README` marker. Containers that must see `/nsls2`
   bind-mount that root (kinetix compose does) — mirroring how beamline
   hosts mount the central store. `seed.sh` now calls it; frame test
   re-PASSES with the bind in place.
   - **Storage policy (AJ concern, 2026-07-30):** sim data volume is
     controlled by construction — the sim camera is 1024² (~1–2 MB/frame; a
     50-projection functional oracle run ≈ 50–100 MB, even 1801 projections
     ≈ 2–4 GB), `/tmp` here is disk-backed (not tmpfs) with ~600 GB free,
     and **full-fidelity volume runs (3200²×16-bit ≈ 36 GB/scan — the
     file-lock regime) are strictly opt-in** on a location with known
     headroom. Working budget ~50 GB under `/tmp/hex-sim-data`:
     `sync_sim_experiment.sh` warns past `HEX_SIM_DATA_WARN_GB` and
     [`scripts/prune_sim_data.sh`](scripts/prune_sim_data.sh) reclaims —
     all sim data is disposable by design. **Future option for volume runs
     (AJ):** `/nsls2/data/tst/proposals/` already exists for TST (31-ID, the
     test beamline) — mounted on the sim host it would be a legitimate home
     for high-volume simulated data, and would exercise the REAL
     central-storage NFS path (the file-lock regime) under a test-beamline
     data session.
   - Equivalence-ledger note: `tomo_flyscan.py` HARDCODES
     `proposal="pass-319162"`/`cycle` — a known pyepics defect (the modern
     process takes identity from Redis via sync-experiment); the hextools
     port fixes it. The oracle run executes verbatim inside a container with
     the `/nsls2` bind, so its hardcoded folder names land in marked sim
     storage.

### 7. 🎯 `tomo_flyscan.py` RUNS VERBATIM against the sim (✅ 2026-07-30)
**The HXM-1288 acceptance-oracle "before" leg is operational.** Final run:
`-n 361 -e 0.015` → "All done!!!"; `panda.hdf` **`Angle` = 361 rows,
0.035°→179.965°, mean step 0.4998°** (measured by the sim encoder through the
real position-armed time train — the 0.035° offset is trigger latency);
**361 real camera frames** (1024², production HDF plugin); complete NeXus
(`rotation_angle` from the PandA, 11 static-motor groups, information).
- **Oracle runner**: [`oracle/Dockerfile`](oracle/Dockerfile) — beamline-era
  env (py3.10, h5py 3.8, numpy 1.24), `PYTHONPATH=/hex-acq-pyepics`
  (beamline convention), `/nsls2` = the sim tree bind, loopback-only CA.
- **Bring-up additions** (all in the pre-flight below): panda-ioc CA moved to
  **:5095** (a second EPICS server on :5064 beside sim_ioc goes deaf);
  `sim_ioc` runs with
  `BLACKHOLE_EXCLUDE_PREFIXES="XF:27ID1-BI{Kinetix-Det:1} XF:27ID1-ES{PANDA:1} XF:27IDF-OP:1{MC:5-"`;
  shutter Sts PVs seeded =1 (CI-harness style);
  [`iocs/panda/init_panda_ioc.py`](iocs/panda/init_panda_ioc.py) restores
  IOC-level autosave-equivalents (`CALC2:OUT:DATASET=Angle`).
- **Sim fixes the oracle forced** (each a fidelity upgrade):
  1. Motor `.DESC` populated (empty desc → empty HDF key → losa crash).
  2. **Real put-completion on the motor** (`motor_sim.py`): a real motor
     record holds `caput(VAL, wait=True)` until the move finishes; the
     instant-complete FakeMotor let `move_to_position()` return early, so
     `get_enc_value()` read the PREVIOUS position and programmed a wrong
     PCOMP start (captured 30 rows at the stop angle). Now sequence-token
     completion tied to the ramp — timeline-verified, no early release, no
     stale-value bounce.
  3. Bridge slew ceiling raised (100 Hz × 150 counts = 75 °/s > the script's
     60 °/s max; the old 12.5 °/s ceiling halved a 25 °/s scan's angle range).
  4. `COUNTER3.TRIG=PULSE1.OUT` added to the design (matches the real box's
     1801 tally — and works around an engine quirk: with PCAP as PULSE1.OUT's
     only listener, the train died after one edge).
- **pyepics defects found by the sim** (functional-equivalence ledger — the
  hextools port should fix, not reproduce):
  1. `tomo_flyscan.py` velocity-clamp branch: `scan_time = |stop−start| × velo`
     (should be ÷) → for parameter sets hitting max_velo the acq_period
     becomes absurd (saw "scanning time 10800 s"); normal 1801-projection
     runs never enter the branch, which is why it survived at the beamline.
  2. Hardcoded `proposal`/`cycle` (already logged; modern flow = Redis via
     sync-experiment).
- All six repo tests PASS after the changes (capture ×3, kinetix frames,
  motor motion, localguard).

### 8. Blackhole asyn-port fix (✅ 2026-08-03)
Latent defect reported by a sibling beamline sim (their blackhole hit it live):
`spoof_beamline.py` fabricated `PortName`/`ArrayPort` PVs as *their own PV name*,
so every driver/plugin reported a unique asyn port — legacy ophyd's
`validate_asyn_ports` needs a plugin's `NDArrayPort` to name an existing driver
`PortName`, so any classic-ophyd AreaDetector device against the blackhole fails
validation. Never triggered at HEX (the only legacy-AD startup files —
11-perkin-elmer, 13-smpl-align-cam — are disabled; Kinetix/PandA/motor are
excluded prefixes), but it was a landmine for enabling those or reusing the
blackhole in the PDF/XPD sims. Fix: all port PVs fabricate ONE consistent name
(`SIM_ASYN_PORT`, default `SIM1`, env `BLACKHOLE_ASYN_PORT`);
`tests/blackhole_fabrication_test.py` grew an asyn-port consistency case — PASS.
Takes effect in the live sim on the next `sim_ioc` restart. **Feed upstream**:
the same flaw exists verbatim in NSLS2/test-beamline-profiles' spoof IOC.
(The sibling's second finding — caproto *client* subscriptions registered before
first connect never activate — does not apply here: our only monitoring client,
the motor bridge, is pyepics; caproto is server-side only in this sim.)

### 9. TTL trigger bridge — the last wire, closed (✅ 2026-08-03)
Prompted by Jakub's "how does the trigger reach the detector?" and AJ's "for
completeness": [`iocs/panda/ttl_trigger_bridge.py`](iocs/panda/ttl_trigger_bridge.py)
(sibling of the motor bridge) camonitors the **cumulative pulse tally**
`COUNTER3:OUT` (= `PULSE1.OUT` in the real design) over CA and pokes one
`cam1:Acquire` per new pulse — cumulative counter, so missed updates heal by
the delta; counter reset on ARM (`COUNTER3.ENABLE` follows `PCAP.ACTIVE`)
rebaselines silently. **OPT-IN**: camera must be `ImageMode=Single`;
never run it under the free-running pyepics oracle (double-drive).
- VERIFIED: `tests/ttl_bridge_test.py` PASS — real motor move → INENC bridge
  → PCOMP/PULSE1 → 5 pulses tallied → 5 Acquire pokes → camera
  `ArrayCounter_RBV` +5 (exactly one frame per pulse, real AD IOC).
- Full smoke suite re-PASS after the change (slowmove, ioc_hdf, kinetix
  frames, blackhole fabrication).

### 10. PandA design as declarative YAML — single source for sim + hextools (✅ 2026-08-03)
Per `dec:panda-configurations` (Jakub's xpdtools pattern):
`hex-ob/hextools/src/hextools/panda_configurations/tomo_radio_1_config.yaml`
(branch `hxm1288-panda-config`, commit `ce38cf2`) holds the recovered
`Tomo_radio_1_config` wiring + calibration as xpdtools-shape
`block.n.field: value` entries. `hex_tomo_design.py --yaml PATH` now applies
the design FROM the YAML (control-protocol commands; `*_dataset` keys routed
to the IOC `:DATASET` PVs over CA — subsumes init_panda_ioc's CALC2 line).
- VERIFIED: YAML→command mapping is **set-identical** to the proven built-in
  DESIGN list; applied live to the sim; slowmove/ioc_hdf/ttl-bridge tests
  re-PASS against the YAML-applied design.
- The profile boot does NOT configure the PandA (it only connects `panda1`);
  configuration is bring-up's job today and the hextools scan-setup's job
  later (M6) — both now have the same YAML to read.

### 11. Kinetix personality — typed device connects to the FRAME tier (✅ 2026-08-03)
The ladder's two Kinetix rungs merge: Det:1 (real AD IOC, real frames) now also
accepts the **typed** ophyd-async `KinetixDetector` — unblocking Nghia's
`alignment_scan` (uses `make_kinetix(detector_id=1)`) against the sim.
Probe-driven (introspected the typed device's 92-PV demand, diffed against the
IOC): only TWO gaps were real — ophyd-async compares enum choices as SETS, and
`ADBaseDataType` is a SupersetEnum, so the DataType/EnableCallbacks "mismatches"
were false alarms.
- `init_kinetix.py` extends the real IOC's `cam1:TriggerMode(_RBV)` mbbo states
  at runtime → {Internal, Rising Edge, Exp. Gate} (driver ignores the semantic
  difference in sim — frames come from Acquire/ImageMode).
- `sim_ioc.py --kinetix-overlay-ids 1` (new default) serves the Kinetix-only
  PVs the real IOC lacks (`cam1:ReadoutPortIdx`+`_RBV`) — **no CA conflict**:
  the real IOC never answers searches for them.
- VERIFIED: `tests/kinetix_typed_connect_test.py` PASS (real typed connect,
  typed read, round-trip write; runs in the profile pixi env); kinetix frames,
  slowmove capture, blackhole fabrication all re-PASS. sim_ioc restarted with
  the overlay; shutter Sts PVs re-seeded (blackhole PVs reset on restart).

### 12. hex-ob `alignment_scan` (ophyd-async 0.19) runs green vs the sim (✅ 2026-08-07)
First hex-ob-native plan verified end-to-end: `lib/detectors.py` +
`plans/tomography/alignment_scan.py` (ported to 0.19.4 on hex-ob branch
`alignment-scan-ophyd-async-019`) drive a full RunEngine scan against the sim —
5 projections (`primary` stream) + 2 flats (`flat` stream), one 7-frame HDF
under the `pass-000000` tree, shutter open/close via blackhole cmd PVs,
rotation restored. Runner: hex-ob `tests/alignment_scan_sim_test.py`
(loopback-guarded). Repeatable after a fresh `up_all.sh`. Gaps closed:
- **`sample_x` promoted from blackhole to a real FakeMotor record**
  (`motor_sim.DEFAULT_MOTOR_PVS` += `SMPL:1-Ax:X1`): the typed ophyd-async
  `Motor` demands enum/string record fields (`.FOFF`/`.OUT`/`.SET`) that the
  blackhole fabricates as floats → `TypeError ... cannot be coerced` on
  connect. motor_ioc + sim_ioc restart picks it up (sim_ioc auto-excludes the
  namespace from blackhole).
- **CA-context gotcha for standalone (non-profile) scripts** — not a sim bug:
  instantiating a `RunEngine` imports pyepics, and ophyd-async's CA backend
  then attaches worker threads to pyepics' *initial context*
  (`_use_pyepics_context_if_imported`). If pyepics never did CA on the main
  thread, that context doesn't exist and every ophyd-async CA connect times
  out (`NotConnectedError`; or `CASeverityException: Thread is already
  attached` if aioca ran in that thread first). Fix: `import epics.ca;
  epics.ca.initialize_libca()` first thing on the main thread. The profile
  boot never hits this because `03-motors.py`'s classic-ophyd signals do
  main-thread CA before any ophyd-async connect. Candidate **upstream
  ophyd-async issue** (the helper should verify the initial context exists).
- `up_all.sh` panda init check made robust: a healthy 7-day-old sim once
  aborted on a transient docker-logs hiccup; a panda-ioc container up >5 min
  now counts as initialized.
- **Fidelity direction (AJ, 2026-08-07):** `~/git_projects/caproto`'s shipped
  IOCs (`caproto.ioc_examples` — fake motor record, and many more) are the
  ready library for replacing further blackhole stand-ins with real records
  as hex-ob scripts demand them (same route as FakeMotor). Goal: the sim
  evolves in lock-step with hex-ob / hextools / hex-profile-collection.

## 🚧 Remaining / next

1. **The "after" leg**: hextools `tomo_fly` via the tutorial (M0→M6), then
   the functional-equivalence comparison against oracle outputs
   (`ver:equivalence-validation` in the design graph).
2. **Real-beamline testing (protocol set 2026-08-03, access pending —
   `dec:real-test-data-home`)**: write tests run on **xf27id1-ws1 / ws3**
   (`module load hex1` → change_energy, view_data, script_runner, edxd_viewer,
   copy_scripts, …; setup per the [HEX wiki user guide](https://wiki-nsls2.bnl.gov/beamline27ID/index.php?title=User_guide#Setup_bash_environment_for_using_software_installed_at_HEX)).
   ALL test data goes under `/nsls2/data/hex/proposals/commissioning/pass-314022`
   (inspect with `view_data`); **never the assets folder — it cannot be
   deleted**. AJ is being added to the commissioning proposal (until then
   `view_data` gives Permission denied). This narrowly amends the standing
   read-only rule below; the sim keeps pass-000000 under /tmp — the two never mix.
3. **Queued**: PandABlocks-webcontrol against the sim (repo already cloned);
   TST storage for volume runs.
4. **Long-term (AJ 2026-08-03, no effort yet — `req:service-layer-path`)**:
   exercise the sim + hex-ob code through the service layers, beyond the
   traditional bsui boot: first **bluesky-queueserver** (every beamline incl.
   HEX already runs its own queueserver VM service), later the
   **ophyd-service** stack (`~/git_projects/ophyd-service`: config /
   direct-control / queueserver backends + React frontend — upstream warns
   "active development, not ready for use"; revisit if the tutorial gets far
   along).
   - Environment scaffolding for the script itself: local
     `/nsls2/data/hex/proposals/2026-2/pass-319162/…` dirs, `sim_ioc.py`
     running (shutter/metadata PVs via blackhole), and verify the fabricated
     shutter-status enums read as "open" to `deco.check_front_end_status()`.

**Queued idea (later increment): PandABlocks-webcontrol against the sim.**
The web GUI AJ captured the design from (PandA Web Control 3.0,
PandABlocks-webcontrol on GitHub) talks the same TCP control protocol our sim
serves via the real PandABlocks-server — so running the webcontrol container
pointed at the sim should give the full graphical layout/value editor driving
the simulated engine. Zero simulator work, high tutorial value (watch PCOMP
fire live in M6). Take up after the pyepics scripts run.
   Also needed to even start: `/nsls2/data/hex/proposals/2026-2/pass-319162/…`
   dirs locally, `sim_ioc.py` running (Kinetix + blackhole for shutter/motor
   PVs), and note the script free-runs `deco.check_front_end_status()` etc.
   against blackhole-fabricated values.

## Bring-up (pre-flight each session)
```bash
cd hex-ob/hex-simulated-beamline
mkdir -p /tmp/hex-sim-data && chmod 777 /tmp/hex-sim-data
# 0. services + experiment identity (Redis/Mongo/Kafka/Tiled; sync-sim + storage tree)
./scripts/up.sh
# 1. panda sim (engine, injection :9101) + pandablocks-ioc (CA :5095) — compose owns BOTH
docker compose -f compose/docker-compose.panda.yml up -d --build
# 2. Kinetix frame tier (real AD IOC, CA :5085)
docker compose -f compose/docker-compose.kinetix.yml up -d
# 3. motor IOC (:5075)
EPICS_CAS_SERVER_PORT=5075 python iocs/motor/motor_ioc.py &
# 4. block design + IOC-level inits (rerun after any container recreate)
#    (--yaml <hex-ob>/hextools/src/hextools/panda_configurations/tomo_radio_1_config.yaml
#     applies the same design from the shared declarative YAML — see §10)
python iocs/panda/hex_tomo_design.py
python iocs/panda/init_panda_ioc.py      # CALC2:OUT:DATASET=Angle
python iocs/kinetix/init_kinetix.py      # cam ArrayCallbacks=1
# 5. motor→INENC bridge (slew-limited; drains replies)
EPICS_CA_ADDR_LIST=127.0.0.1:5075 python -u iocs/panda/motor_encoder_bridge.py &
# 6. sim_ioc: shutters/metadata via blackhole + typed Det:3 (real IOC owns Det:1)
#    + Kinetix-personality overlay for Det:1 (§11: cam1:ReadoutPortIdx etc.)
BLACKHOLE_EXCLUDE_PREFIXES="XF:27ID1-BI{Kinetix-Det:1} XF:27ID1-ES{PANDA:1} XF:27IDF-OP:1{MC:5-" \
  pixi run --manifest-path <hex-profile-collection>/pixi.toml -e terminal \
  python iocs/sim_ioc.py --kinetix-ids 3 --kinetix-overlay-ids 1 &
# 7. seed shutter status open (blackhole PVs are writable, CI-harness style)
#    caput XF:27IDA-PPS{Sh:FE}Sts:OpnCmd-Sts 1 ; caput "XF:27IDA-PPS{L1-S1}Sts:OpnCmd-Sts" 1
# 8. smoke: the test suite should be all-PASS (~90 s)
python iocs/panda/tests/slowmove_capture_test.py
python iocs/panda/tests/ioc_hdf_capture_test.py
python iocs/panda/tests/kinetix_frames_test.py
#    typed-connect check needs ophyd-async (profile pixi env, not /tmp/pbenv):
pixi run --manifest-path <hex-profile-collection>/pixi.toml -e terminal \
  python iocs/panda/tests/kinetix_typed_connect_test.py
# 9. the ORACLE (pyepics tomo_flyscan.py, verbatim; see oracle/Dockerfile header)
docker build -t hexsim-oracle:local oracle/   # once
docker run --rm --network host \
  -v <hex-acq-pyepics>:/hex-acq-pyepics:ro -v /tmp/hex-sim-data/nsls2:/nsls2 \
  -e PYTHONPATH=/hex-acq-pyepics -e EPICS_CA_AUTO_ADDR_LIST=NO \
  -e "EPICS_CA_ADDR_LIST=127.0.0.1:5064 127.0.0.1:5075 127.0.0.1:5085 127.0.0.1:5095" \
  hexsim-oracle:local python /hex-acq-pyepics/techniques/tomography/kinetix/tomo_flyscan.py -n 361 -e 0.015
```
Test env used: `/tmp/pbenv` (numpy, jinja2, p4p, pyepics, caproto, pandablocks,
h5py). Standing rules: real beamline READ-ONLY — with ONE sanctioned exception
(2026-08-03): write tests from ws1/ws3 into the commissioning proposal
`/nsls2/data/hex/proposals/commissioning/pass-314022`, never assets
(`dec:real-test-data-home`); sim writes only otherwise; hxm_program
direct main OK + `tools/check_repo.py` + `Assisted-by:` trailer; push BOTH
remotes (upstream NSLS2 + origin fork).
