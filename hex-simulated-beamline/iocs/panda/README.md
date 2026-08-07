# Simulated PandA (real PVA/PVI — route "b", no mocking)

Gives the HEX profile's `HDFPanda` a **real PVA `:PVI` interface** to introspect, so PandA doesn't have to be
mocked. Two pieces (wired in [`../../compose/docker-compose.panda.yml`](../../compose/docker-compose.panda.yml)):

- **`panda-sim`** — the **PandABlocks-server** driven by the **PandABlocks-FPGA cycle-accurate block-sim
  engine** (not the server's bundled register echo). Built by [`Dockerfile.simserver`](Dockerfile.simserver)
  as a two-stage image: stage 1 clones **PandABlocks-FPGA** and autogenerates the app `config_d` (pure Python,
  `common.python.generate_app` — no Vivado); stage 2 builds the server `sim_server` (pinned `4.1`) and runs
  `simserver -f` against our Python-3 engine wrapper ([`fpga_sim_server.py`](fpga_sim_server.py)). Speaks the
  PandA control protocol on **:8888** (config) / **:8889** (data). *(Using a real `pandabox-no-fmc` app config
  avoids the bundled minimal **test** config, whose artifacts — an undescribed `INTERVAL` block, a bogus
  `*METADATA.LABEL_BLAH1`, and zero-length tables — break ioc introspection.)*

  **Why the engine ("Option C").** The server's bundled `python/sim_server` is "as dumb as a brick": it answers
  the register protocol but runs **no block logic**, so `CLOCK`/`PCOMP`/`PULSE`/`PCAP` never tick and
  `PCAP.ACTIVE` never latches on `*PCAP.ARM=` — an ophyd-async fly scan (arm → wait `pcap.active` → capture)
  stalls. PandABlocks-FPGA ships a real block-sim engine (`common/python/simulations.py` + per-block
  `*_sim.py`); `fpga_sim_server.py` fronts it with a Python-3 register socket loop (the shipped autogen sim
  server is Python-2) and is selected via `simserver -f`. Two fixups it applies: it drops the FPGA-only
  `sfp_panda_sync` blocks (need pin-constraint metadata absent in a soft build), and it re-derives the `*REG`
  PCAP register offsets from the generated `registers` file (the engine hardcodes `ARM=10`, but this app
  places it at `13`) and maps them to the bare names `pcap_sim` acts on — without this, `*PCAP.ARM=` is
  silently dropped.
- **`panda-ioc`** — the published **`pandablocks-ioc`** (pinned `0.11.5`, matching the `nsls2.ioc_deploy`
  `pandabox` role). Connects to `panda-sim` and serves EPICS **CA + PVA** for prefix `XF:27ID1-ES{PANDA:1}`
  (its PVXS/QSRV2 PVA server publishes the `:PVI` structure `HDFPanda` introspects).

Both run with **host networking** so CA/PVA reach a bluesky session on the host. The ioc runs `softioc`, which
drops into an interactive console — the compose service sets `stdin_open: true` so it doesn't hit EOF and
restart-loop.

## Run

```bash
cd hex-ob/hex-simulated-beamline
# build the sim image (first time only; ~1 min)
docker build -f iocs/panda/Dockerfile.simserver -t hexsim-panda-sim:local iocs/panda
# start alongside the services stack
docker compose -f compose/docker-compose.yml -f compose/docker-compose.panda.yml up -d
```

## Status (2026-07-27) — ✅ engine-backed sim; `PCAP:ACTIVE` latches on arm

- ✅ **sim builds + runs** with the real `pandabox-no-fmc` config and the FPGA block-sim engine —
  C server reports `Server started`, listening on `:8888`/`:8889`.
- ✅ **`pandablocks-ioc` fully initializes** against it — `iocRun: All initialization complete`.
- ✅ **`PCAP:ACTIVE` latches TRUE on ARM over PVA** (p4p): configure a capture field + `PCAP:ENABLE`, put
  `PCAP:ARM=1`, and `PCAP:ACTIVE` reads `1` — the exact `set_and_wait_for_other_value(pcap.arm, True,
  pcap.active, True)` gate ophyd-async's arm blocks on. The dumb bundled sim never did this.

Benign warnings only: a few block descriptions exceed the EPICS 40-char limit (truncated), and
`SYSTEM:TEMP_ZYNQ`/`VCCINT` (hardware sensors) read invalid — neither affects device/plan work.

### ✅ Triggered frame capture works end-to-end (2026-07-28)

Arming, position-compare capture during a **live motor move**, and the full
pandablocks-ioc data path (`DATA:NumCaptured`, HDF `Angle` dataset) are all
verified — see [`tests/`](tests/) (three PASS/FAIL scripts) and
[`../../PROGRESS.md`](../../PROGRESS.md). Three sim-side defects had to fall:
the engine dropped pending wakeups when changes collided (merged now), a
blocking injection-reply `sendall` could wedge the server (non-blocking now,
and the bridge slew-limits + drains replies), and register writes lost the
sign on ini-declared-signed fields (`param int`), so the real encoder offset
(−39660) pushed PCOMP thresholds out of reach (`install_signed_writes()`).

### Motor → INENC position bridge

The sim motor (a separate caproto `FakeMotor` IOC, `:5075`) has no electrical link
to the PandA, so PandA never sees the motion. Three pieces close that loop so
`PCOMP → PULSE → PCAP` capture fires as the stage sweeps:

- **`fpga_sim_server.py`** opens a **position-injection channel** on
  `127.0.0.1:9101` (env `PANDA_INJECT_PORT`). Line protocol `"<BLOCK> <FIELD>
  <int>"` (e.g. `INENC1 VAL 1600`); each line is applied to the engine so it fans
  out to `CALC`/`PCOMP` listeners.
- **[`hex_tomo_design.py`](hex_tomo_design.py)** wires the tomo design over the
  control protocol (the bare `pandabox-no-fmc` app has all muxes `ZERO`):
  `INENC1.VAL → CALC2` (identity; `CALC2:OUT` = `get_enc_value`) and `PCOMP1.INP`;
  `PCOMP1.OUT → PULSE1.TRIG → PULSE1.OUT → PCAP.TRIG`; `BITS.A` is the software run
  gate; `CALC2.OUT` is captured (the Angle).
- **[`motor_encoder_bridge.py`](motor_encoder_bridge.py)** (host): camonitors the
  motor `RBV` and injects encoder counts (`deg * counts_per_deg + offset`,
  defaults `200` / `-39660`) into `INENC1.VAL`.

Verified: a fine injected sweep captures 5 frames (encoder value at each `PCOMP`
compare point), and moving the real motor to 8° drives `CALC2:OUT = 1600`.


### Version pins / knobs

- `PANDA_SERVER_REF=4.1` (server), `PANDA_FPGA_REF=main` + `PANDA_APP=pandabox-no-fmc` (config) — override via
  `--build-arg`. `pandablocks-ioc` `0.11.5` in the compose overlay.

## Networking note (blackhole overlap)

The CA-only **blackhole** answers *all* PVs, including PandA CA PVs — so if both run, PandA **CA** reads are
ambiguous. This is fine for the profile because `HDFPanda` uses **PVA** (served only by `panda-ioc`), which is
unambiguous. When wiring the full profile, scope the blackhole as a *last-resort* fallback (or don't let it
shadow PVs a real sim IOC serves).
