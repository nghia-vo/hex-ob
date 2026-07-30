# Quickstart — simulated HEX beamline + this sandbox

For anyone (Nghia, future-AJ, …) who wants to run the simulated HEX beamline
and develop hextools / hex-profile-collection against it. Deep detail lives in
[`hxm_program/simulated_beamlines/HEX/PROGRESS.md`](https://github.com/NSLS2/hxm_program/blob/main/simulated_beamlines/HEX/PROGRESS.md);
this page is the 5-minute version.

## 0. What you get

A hardware-free HEX (27-ID): fake services (TLS Redis :6380, Mongo, Kafka,
Tiled), the real PandABlocks stack running the recovered production tomography
design `Tomo_radio_1_config` (arm-gated position-compare launching a time
train), a REAL AreaDetector IOC producing frames as Kinetix-Det:1, a simulated
rotation motor with true put-completion, and a blackhole for every incidental
PV. The legacy pyepics `tomo_flyscan.py` runs against it **verbatim** and
produces a full scan (frames + `Angle` in degrees + NeXus) — that output is the
functional-equivalence oracle for the hextools port (DECISIONS.md D-0014).

Safety by construction: everything binds 127.0.0.1; sim EPICS clients refuse
non-loopback address lists (`localguard`); simulated data lives only under
`/tmp/hex-sim-data` with a sentinel data session (`pass-000000`) — real
beamline hosts and real proposal storage are unreachable.

## 1. Bring up the whole sim (one command)

```bash
cd ~/git_projects/hxm_program/simulated_beamlines/HEX
./scripts/up_all.sh          # idempotent; auto-creates a helper venv on first run
source scripts/env.sh        # loopback-only EPICS client env (4 CA ports)
```

One-time prerequisite it will tell you about: the Kinetix AD IOC image
(`hexsim-kinetix-ioc:local`) is built via `nsls2.ioc_deploy` — steps in
PROGRESS.md ("Kinetix frame tier").

Smoke test (should PASS): `.toolenv/bin/python iocs/panda/tests/slowmove_capture_test.py`

## 2. Boot the profile collection against the sim

```bash
cd <your hex-profile-collection>     # branch hxm-1288-sim-boot (HEX_SIM gate)
source ~/git_projects/hxm_program/simulated_beamlines/HEX/scripts/env.sh
export HEX_SIM=1 MPLBACKEND=Agg
pixi run -e terminal ipython -- --profile-dir=.     # → BOOT COMPLETE
```

`--profile-dir=.` is what executes `startup/*.py` (the mechanism `bsui` wraps).
`panda1` connects over real PVA; `kinetix3` is the typed caproto sim;
`kinetix1` is the real AD IOC (it lacks Kinetix-specific PVs, so the typed
ophyd-async device may report it unavailable — expected).

## 3. Run the pyepics oracle (the "before" reference)

```bash
cd ~/git_projects/hxm_program/simulated_beamlines/HEX
docker build -t hexsim-oracle:local oracle/     # once
docker run --rm --network host \
  -v ~/git_projects/hex-acq-pyepics:/hex-acq-pyepics:ro \
  -v /tmp/hex-sim-data/nsls2:/nsls2 \
  -e PYTHONPATH=/hex-acq-pyepics -e EPICS_CA_AUTO_ADDR_LIST=NO \
  -e "EPICS_CA_ADDR_LIST=127.0.0.1:5064 127.0.0.1:5075 127.0.0.1:5085 127.0.0.1:5095" \
  hexsim-oracle:local \
  python /hex-acq-pyepics/techniques/tomography/kinetix/tomo_flyscan.py -n 361 -e 0.015
```

Outputs land under
`/tmp/hex-sim-data/nsls2/data/hex/proposals/.../tomography/raw_data/scan_NNNNN/`
(`panda.hdf` `Angle` in degrees, `proj_00000.hdf` frames, `.nxs` metadata).
Avoid parameter sets where the velocity clamp engages (a known script bug —
see PROGRESS "pyepics defects").

## 4. Develop in this sandbox

`hextools/` and `hex-profile-collection/` here are full clones (own git,
branches, remotes) — see [`README.md`](README.md) for the contract: develop
freely, test against the sim, merge back to the NSLS2 repos via PRs only.
Recreate envs in place: `pixi install` / `uv sync`.

## Housekeeping

* Prune sim data anytime: `scripts/prune_sim_data.sh --yes` (all disposable).
* Tear down: `scripts/down.sh` + `docker compose -f compose/docker-compose.panda.yml down`
  + same for kinetix; kill the `motor_ioc`/`bridge`/`sim_ioc` processes.
* Rerun `./scripts/up_all.sh` after any container recreate — it reapplies the
  PandA design and the IOC-level init settings.
