# Simulated HEX (27-ID) beamline

Hardware-free dev/test stack for HEX ophyd-async devices + Bluesky plans, and the acceptance
oracle for the **HXM-1288** hextools tomography port. **Interim home** (see
[`../README.md`](../README.md), I-015); governed by [`../../DECISIONS.md`](../../DECISIONS.md)
**D-0013/D-0014** (device-first cadence; minimal sim per increment, pyepics scripts = the
executable oracle).

**This file is the stable front door** — what the sim is and how to run it.
**History and current increment live in [`PROGRESS.md`](PROGRESS.md)** (the running log);
the guided tutorial lives in
[`../../planning/tasks/hex/HXM-1288.tutorial-tomography.md`](../../planning/tasks/hex/HXM-1288.tutorial-tomography.md).

**Status (2026-08-03):** the "before" leg is operational — `tomo_flyscan.py` (pyepics) runs
**verbatim** against the sim: 361 real camera frames, PandA `Angle` dataset in degrees, full
NeXus. Tutorial M0 done, M1 open. Next up: the hextools "after" leg (M2–M6), then the
equivalence gate.

## Architecture (all on 127.0.0.1 — nothing leaves the host)

```
                 Bluesky / ophyd-async session  ──or──  oracle container (pyepics, verbatim)
                        │ CA + PVA                            │ CA
   ┌────────────────────┼─────────────────────────────────────┼──────────────────────────┐
   │                    ▼                                     ▼                          │
   │  :5064  sim_ioc.py — ONE unified CA server: typed Det:3 sims + blackhole fallback   │
   │         (BLACKHOLE_EXCLUDE_PREFIXES keeps it off Det:1 / PANDA / MC:5 motor)        │
   │  :5075  motor_ioc.py — caproto FakeMotor, real ramps + DMOV, put-completion         │
   │  :5085  hexsim-kinetix-ioc — REAL AreaDetector IOC (ADSimDetector via               │
   │         nsls2.ioc_deploy), real 1024² frames + production HDF plugin               │
   │  :5095  panda-ioc (pandablocks-ioc 0.11.5) — CA + PVA (:PVI) for HDFPanda           │
   │            │ control protocol                                                       │
   │            ▼                                                                        │
   │  panda-sim — PandABlocks-server driven by the FPGA block-sim ENGINE (Option C),     │
   │         wired to the real Tomo_radio_1_config design; position inject :9101         │
   │            ▲                                                                        │
   │  motor_encoder_bridge.py — camonitors motor RBV → INENC1 counts (slew-limited)      │
   └─────────────────────────────────────────────────────────────────────────────────────┘
   services: Redis :6380 (TLS) · Mongo :27017 · Kafka :9092 · Tiled :8000 (api key `secret`)
   data: /tmp/hex-sim-data (incl. the /nsls2 mimic — sim data session pass-000000 ONLY)
```

**Fidelity tiers** (per-device, grown on demand — D-0014):

| Tier | Serves | Gets you | Doesn't |
|---|---|---|---|
| Frame (real IOC) | Kinetix **Det:1** :5085 | real frames, HDF files, plugin chain | Kinetix-specific PVs (typed connect refuses it — by design) |
| Device-connect (typed sim) | Kinetix **Det:3** in :5064 | exact typed PV set, clean ophyd-async connect | frames (a `count()` fails loud on `NumCaptured`) |
| Blackhole | every incidental PV | plausibly-typed fabrication, writable | device-specific behavior |
| Engine (PandA) | :5095/PVA + control | real block logic: PCOMP fires, PCAP captures | FPGA-only blocks (`sfp_panda_sync` dropped) |

## How signals travel (the wires question)

*Visual one-pager: [`signal_paths.html`](signal_paths.html) (open in a browser).*

Two different mechanisms, and knowing which is which is the key to trusting the sim:

- **Inside the PandA — exact.** The engine is the PandABlocks-FPGA cycle-accurate block
  simulation running the beamline's real `Tomo_radio_1_config` design, so block-to-block
  signals are tracked for real: PCOMP1 fires at the programmed angle, PULSE1 emits the
  N-pulse train, `TTLOUT1` asserts per pulse, `COUNTER3` tallies the train, PCAP captures
  the angle each trigger fired at.
- **Between boxes — software bridges replace electrical wires.**
  - *Motor encoder → PandA INENC1*: **bridged** (verified). `motor_encoder_bridge.py`
    camonitors the motor RBV, converts with the real calibration (raw = 39660 − 200·deg),
    and injects into the engine's position bus (TCP :9101), slew-limited so PCOMP can never
    be hopped over.
  - *PandA `TTLOUT1` → camera trigger input*: **bridged, OPT-IN** (verified 2026-08-03).
    `ttl_trigger_bridge.py` camonitors the cumulative pulse tally (`COUNTER3:OUT` =
    `PULSE1.OUT`) and pokes one `cam1:Acquire` per pulse (camera in `ImageMode=Single`) —
    exactly one frame per PandA trigger. Run it only for triggered-mode work: the
    free-running pyepics oracle must NOT have it active (it would double-drive the camera),
    and its totals-based checks don't need it.

## Quickstart

```bash
cd simulated_beamlines/HEX
./scripts/up_all.sh          # ONE command: services, PandA, Kinetix, motor, bridge,
                             # sim_ioc, design + IOC inits, shutter seeds
```

Then boot the profile against it (from the **hex-ob sandbox clone** — all HXM-1288 work
happens there):

```bash
cd ~/git_projects/hex-ob/hex-profile-collection     # branch hxm-1288-sim-boot
source ~/git_projects/hex-ob/hex-simulated-beamline/scripts/env.sh
export HEX_SIM=1 MPLBACKEND=Agg
pixi run -e terminal ipython --profile-dir=.        # → all startup/*.py → pass-000000 prompt
```

Boot completion = the `pass-000000 [1]:` prompt (the simulated data session, right where a
real proposal number would sit). Expect: `panda1` connects over PVA, `kinetix3` connects
typed, `kinetix1` reports unavailable (frame tier ≠ Kinetix personality — see the table).

**Smoke tests** (~90 s, all should PASS; run after any container recreate):

```bash
python iocs/panda/tests/slowmove_capture_test.py    # motor → bridge → PCOMP → capture
python iocs/panda/tests/ioc_hdf_capture_test.py     # IOC PVs → HDF Angle dataset
python iocs/panda/tests/kinetix_frames_test.py      # real frames → readable HDF5
```

**The oracle** (pyepics `tomo_flyscan.py`, verbatim, in a beamline-era container): build once
with `docker build -t hexsim-oracle:local oracle/`, run per the header in
[`oracle/Dockerfile`](oracle/Dockerfile) or the pre-flight in [`PROGRESS.md`](PROGRESS.md).

## Safety guardrails (non-negotiable)

- **`localguard.py`**: sim-side EPICS clients refuse to start unless every CA/PVA address is
  loopback — the sim reuses REAL PV names, so the beamline network must be unreachable by
  construction.
- **Sim data session**: identity is the sentinel **`pass-000000`** (proposal type
  `SIMULATED`); the `/nsls2` tree is mimicked ONLY under `/tmp/hex-sim-data/nsls2` (with a
  `_SIMULATED_DATA_README` marker). **Never** create a real-looking `/nsls2` on a host, never
  use a real proposal number.
- Storage budget ~50 GB under `/tmp/hex-sim-data`; `scripts/prune_sim_data.sh` reclaims —
  all sim data is disposable by design.

## Layout

```
HEX/
  README.md                     # this file — the stable front door
  PROGRESS.md                   # running log: how we got here + current increment
  compose/
    docker-compose.yml          # services: Redis(TLS 6380)/Mongo/Kafka/Tiled
    docker-compose.panda.yml    # panda-sim (engine) + panda-ioc (CA :5095, PVA)
    docker-compose.kinetix.yml  # frame tier: real AD IOC (CA :5085, loopback binds)
  configs/                      # kafka.yml / databroker.yml client configs
  iocs/
    blackhole/                  # vendored spoof_beamline.py (+ EXCLUDE_PREFIXES addition)
    sim_devices/                # typed per-detector sims (introspected; kinetix, motor)
    sim_ioc.py                  # unified CA server (:5064): typed sims + blackhole
    motor/motor_ioc.py          # dedicated FakeMotor IOC (:5075)
    kinetix/                    # ioc_deploy config + init_kinetix.py (ArrayCallbacks)
    panda/                      # fpga_sim_server.py (engine wrapper), Dockerfile.simserver,
                                #   hex_tomo_design.py, motor_encoder_bridge.py,
                                #   init_panda_ioc.py, localguard.py, designs/, tests/
  oracle/                       # verbatim pyepics runner (beamline-era py3.10 env)
  scripts/
    up_all.sh                   # one-command full bring-up (idempotent-ish)
    up.sh / down.sh / seed.sh   # services layer only
    env.sh                      # source me: EPICS_CA_ADDR_LIST (:5064 :5075 :5085 :5095),
                                #   TILED_*, KAFKA_*
    sync_sim_experiment.sh      # data-session mimic (pass-000000) + /nsls2 tree under sim root
    prune_sim_data.sh           # reclaim sim storage
```
